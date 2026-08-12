"""Low-latency AllModels TTS through Hermes' streaming speaker pipeline."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Iterator, Optional

from tools.tts_streaming import StreamingTTSProvider, register

from .catalog import CatalogStore
from .client import OPENAI_BASE_URL, AllModelsClient
from .providers import _safe_exception

_MAX_STREAM_BYTES = 16 * 1024 * 1024
logger = logging.getLogger(__name__)


def _streaming_binding(
    catalog: Optional[Dict[str, Any]],
    model_id: str,
    provider_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the selected model/provider binding when it supports streaming."""
    if not catalog or not model_id:
        return None
    models = catalog.get("models")
    entries = models.get("tts") if isinstance(models, dict) else None
    for model in entries if isinstance(entries, list) else []:
        if not isinstance(model, dict):
            continue
        identifiers = {str(model.get("id") or "")}
        aliases = model.get("aliases")
        if isinstance(aliases, list):
            identifiers.update(str(alias) for alias in aliases if isinstance(alias, str))
        if model_id not in identifiers:
            continue
        bindings = model.get("providers")
        candidates = [binding for binding in bindings if isinstance(binding, dict)] if isinstance(bindings, list) else []
        if provider_id:
            candidates = [
                binding
                for binding in candidates
                if str(binding.get("id") or "") == provider_id
            ]
        return next(
            (binding for binding in candidates if binding.get("streaming") is True),
            None,
        )
    return None


class AllModelsStreamingTTSProvider(StreamingTTSProvider):
    """Yield AllModels PCM chunks using Hermes' bundled OpenAI SDK."""

    sample_rate = 24000
    channels = 1
    sample_width = 2

    _client: Optional[AllModelsClient] = None
    _catalog: Optional[CatalogStore] = None

    @classmethod
    def available(cls) -> bool:
        try:
            client = cls._client or AllModelsClient()
            return bool(client.get_api_key())
        except Exception:
            return False

    def __init__(
        self,
        tts_config: Dict[str, Any],
        section: Dict[str, Any],
        *,
        client: Optional[AllModelsClient] = None,
        catalog: Optional[CatalogStore] = None,
    ) -> None:
        super().__init__(tts_config, section)
        self.client = client or type(self)._client or AllModelsClient()
        self.catalog = catalog or type(self)._catalog or CatalogStore(self.client)
        self.model = str(tts_config.get("model") or "").strip()
        self.voice = str(tts_config.get("voice") or "").strip()
        requested_provider = str(section.get("voice_provider") or "").strip()
        cached_catalog = self.catalog.cached()
        binding = _streaming_binding(cached_catalog, self.model, requested_provider)
        if binding is None:
            reason = "catalog_unavailable" if cached_catalog is None else "not_advertised"
            logger.info(
                "AllModels TTS path: synchronous fallback "
                "(model=%s, provider=%s, reason=%s)",
                self.model or "unset",
                requested_provider or "auto",
                reason,
            )
            raise RuntimeError(
                "The selected AllModels model/provider does not advertise streaming TTS."
            )
        self.voice_provider = requested_provider or str(binding.get("id") or "").strip()
        if not self.voice:
            raise RuntimeError("No AllModels voice is selected.")

    def stream(self, text: str) -> Iterator[bytes]:
        key = self.client.get_api_key()
        if not key:
            raise RuntimeError("AllModels is not configured. Run /speech to sign up.")

        from openai import OpenAI

        client = OpenAI(api_key=key, base_url=OPENAI_BASE_URL, timeout=60, max_retries=0)
        started = time.monotonic()
        chunks = 0
        total = 0
        outcome = "interrupted"
        logger.info(
            "AllModels TTS path: streaming PCM (model=%s, provider=%s)",
            self.model,
            self.voice_provider or "auto",
        )
        try:
            kwargs: Dict[str, Any] = {
                "model": self.model,
                "voice": self.voice,
                "input": text,
                "response_format": "pcm",
                "extra_headers": {"x-idempotency-key": str(uuid.uuid4())},
            }
            speed = self.tts_config.get("speed")
            if isinstance(speed, (int, float)) and not isinstance(speed, bool):
                kwargs["speed"] = max(0.25, min(4.0, float(speed)))
            if self.voice_provider:
                kwargs["extra_body"] = {"provider": self.voice_provider}

            with client.audio.speech.with_streaming_response.create(**kwargs) as response:
                for chunk in response.iter_bytes():
                    if not chunk:
                        continue
                    chunks += 1
                    total += len(chunk)
                    if total > _MAX_STREAM_BYTES:
                        raise RuntimeError("AllModels TTS stream exceeded the safe audio limit.")
                    yield chunk
            outcome = "complete"
        except Exception as exc:
            outcome = "failed"
            logger.warning(
                "AllModels TTS stream failed "
                "(model=%s, provider=%s, chunks=%d, bytes=%d, duration=%.2fs, "
                "error_type=%s)",
                self.model,
                self.voice_provider or "auto",
                chunks,
                total,
                time.monotonic() - started,
                exc.__class__.__name__,
            )
            raise RuntimeError(f"AllModels streaming TTS failed: {_safe_exception(exc, key)}") from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
            if outcome == "complete":
                logger.info(
                    "AllModels TTS stream complete "
                    "(model=%s, provider=%s, chunks=%d, bytes=%d, duration=%.2fs)",
                    self.model,
                    self.voice_provider or "auto",
                    chunks,
                    total,
                    time.monotonic() - started,
                )
            elif outcome == "interrupted":
                logger.info(
                    "AllModels TTS stream interrupted "
                    "(model=%s, provider=%s, chunks=%d, bytes=%d, duration=%.2fs)",
                    self.model,
                    self.voice_provider or "auto",
                    chunks,
                    total,
                    time.monotonic() - started,
                )


def register_allmodels_streaming_provider(
    client: AllModelsClient,
    catalog: CatalogStore,
) -> type[AllModelsStreamingTTSProvider]:
    """Bind the active plugin client/catalog and register the Hermes streamer."""

    class BoundAllModelsStreamingTTSProvider(AllModelsStreamingTTSProvider):
        pass

    BoundAllModelsStreamingTTSProvider.__name__ = "BoundAllModelsStreamingTTSProvider"
    BoundAllModelsStreamingTTSProvider._client = client
    BoundAllModelsStreamingTTSProvider._catalog = catalog
    register("allmodels")(BoundAllModelsStreamingTTSProvider)
    return BoundAllModelsStreamingTTSProvider
