"""Native Hermes TTS/STT providers backed by AllModels' OpenAI API."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.transcription_provider import TranscriptionProvider
from agent.tts_provider import TTSProvider

from . import settings
from .catalog import CatalogStore
from .client import OPENAI_BASE_URL, AllModelsClient

logger = logging.getLogger(__name__)


def _safe_exception(exc: BaseException, api_key: str) -> str:
    message = str(exc) or exc.__class__.__name__
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message[:800]


def _eligible_models(
    catalog: Optional[Dict[str, Any]], capability: str
) -> List[Dict[str, Any]]:
    if not catalog:
        return []
    models = catalog.get("models", {}).get(capability, [])
    result: List[Dict[str, Any]] = []
    for model in models if isinstance(models, list) else []:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str):
            continue
        providers = model.get("providers")
        if not isinstance(providers, list):
            continue
        if any(isinstance(p, dict) and p.get("synchronous") is True for p in providers):
            result.append(model)
    return result


class AllModelsTTSProvider(TTSProvider):
    def __init__(self, client: AllModelsClient, catalog: CatalogStore) -> None:
        self.client = client
        self.catalog = catalog

    @property
    def name(self) -> str:
        return "allmodels"

    @property
    def display_name(self) -> str:
        return "AllModels Speech"

    @property
    def voice_compatible(self) -> bool:
        """Opt into Ogg/Opus only where Hermes delivers voice bubbles.

        Hermes uses this property not just as a capability flag, but as a
        request to rewrite the provider's output path to Ogg/Opus.  Doing that
        unconditionally makes local CLI/TUI/desktop voice mode diverge from
        built-in synchronous providers, whose sentence pipeline keeps the MP3
        path it asked for and hands that path to the local player.

        Match Hermes' own destination routing so local voice mode receives the
        requested MP3 while messaging surfaces that require Opus still get a
        native voice bubble.
        """
        from gateway.session_context import get_session_env
        from tools.tts_tool import OPUS_VOICE_PLATFORMS

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        return str(platform or "").strip().lower() in OPUS_VOICE_PLATFORMS

    def is_available(self) -> bool:
        try:
            return bool(self.client.get_api_key())
        except Exception:
            return False

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Unified TTS through AllModels",
            "env_vars": [
                {
                    "key": "ALLMODELS_API_KEY",
                    "prompt": "AllModels API key",
                    "url": "https://allmodels.io",
                }
            ],
        }

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": item["id"], "display": item["id"]}
            for item in _eligible_models(self.catalog.cached(), "tts")
        ]

    def list_voices(self) -> List[Dict[str, Any]]:
        catalog = self.catalog.cached() or {}
        voices = catalog.get("voice_entries")
        result: List[Dict[str, Any]] = []
        if not isinstance(voices, list):
            return result
        for voice in voices:
            if not isinstance(voice, dict) or not voice.get("id"):
                continue
            name = str(voice.get("name") or voice["id"])
            provider = str(voice.get("provider") or "")
            result.append(
                {
                    "id": str(voice["id"]),
                    "display": f"{name} — {provider}",
                    "language": voice.get("language"),
                    "gender": voice.get("gender"),
                    "provider": provider,
                }
            )
        return result

    def default_model(self) -> Optional[str]:
        return None

    def default_voice(self) -> Optional[str]:
        return None

    def synthesize(
        self,
        text: str,
        output_path: str,
        *,
        voice: Optional[str] = None,
        model: Optional[str] = None,
        speed: Optional[float] = None,
        format: str = "mp3",
        **extra: Any,
    ) -> str:
        key = self.client.get_api_key()
        if not key:
            raise RuntimeError("AllModels is not configured. Run /speech to sign up.")
        status = settings.speech_status()
        selected_model = (model or status.get("tts_model") or "").strip()
        selected_voice = (voice or status.get("tts_voice") or "").strip()
        if not selected_model:
            raise RuntimeError(
                "No AllModels TTS model is selected. Run /speech tts model."
            )
        if not selected_voice:
            raise RuntimeError("No AllModels voice is selected. Run /speech tts voice.")

        response_format = str(format or "mp3").lower()
        if response_format == "ogg":
            response_format = "opus"
        if response_format not in {"mp3", "opus", "aac", "flac", "wav", "pcm"}:
            response_format = "mp3"
        selected_provider = str(
            extra.get("voice_provider") or status.get("tts_voice_provider") or ""
        ).strip()

        from openai import OpenAI

        client = OpenAI(
            api_key=key, base_url=OPENAI_BASE_URL, timeout=60, max_retries=0
        )
        started = time.monotonic()
        logger.info(
            "AllModels TTS path: synchronous file (model=%s, provider=%s, format=%s)",
            selected_model,
            selected_provider or "auto",
            response_format,
        )
        try:
            kwargs: Dict[str, Any] = {
                "model": selected_model,
                "voice": selected_voice,
                "input": text,
                "response_format": response_format,
                "extra_headers": {"x-idempotency-key": str(uuid.uuid4())},
            }
            if speed is not None:
                kwargs["speed"] = max(0.25, min(4.0, float(speed)))
            if selected_provider:
                kwargs["extra_body"] = {"provider": selected_provider}
            response = client.audio.speech.create(**kwargs)
            target = Path(output_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            response.stream_to_file(target)
            try:
                audio_bytes = target.stat().st_size
            except OSError:
                audio_bytes = 0
            logger.info(
                "AllModels TTS synchronous complete "
                "(model=%s, provider=%s, format=%s, bytes=%d, duration=%.2fs)",
                selected_model,
                selected_provider or "auto",
                response_format,
                audio_bytes,
                time.monotonic() - started,
            )
            return str(target)
        except Exception as exc:
            logger.warning(
                "AllModels TTS synchronous failed "
                "(model=%s, provider=%s, format=%s, duration=%.2fs, error_type=%s)",
                selected_model,
                selected_provider or "auto",
                response_format,
                time.monotonic() - started,
                exc.__class__.__name__,
            )
            raise RuntimeError(
                f"AllModels TTS failed: {_safe_exception(exc, key)}"
            ) from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()


class AllModelsTranscriptionProvider(TranscriptionProvider):
    def __init__(self, client: AllModelsClient, catalog: CatalogStore) -> None:
        self.client = client
        self.catalog = catalog

    @property
    def name(self) -> str:
        return "allmodels"

    @property
    def display_name(self) -> str:
        return "AllModels Transcription"

    def is_available(self) -> bool:
        try:
            return bool(self.client.get_api_key())
        except Exception:
            return False

    def get_setup_schema(self) -> Dict[str, Any]:
        return {
            "name": self.display_name,
            "badge": "paid",
            "tag": "Unified STT through AllModels",
            "env_vars": [
                {
                    "key": "ALLMODELS_API_KEY",
                    "prompt": "AllModels API key",
                    "url": "https://allmodels.io",
                }
            ],
        }

    def list_models(self) -> List[Dict[str, Any]]:
        return [
            {"id": item["id"], "display": item["id"]}
            for item in _eligible_models(self.catalog.cached(), "stt")
        ]

    def default_model(self) -> Optional[str]:
        return None

    def transcribe(
        self,
        file_path: str,
        *,
        model: Optional[str] = None,
        language: Optional[str] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        key = self.client.get_api_key()
        if not key:
            return {
                "success": False,
                "transcript": "",
                "error": "AllModels is not configured. Run /speech to sign up.",
                "provider": self.name,
            }
        status = settings.speech_status()
        selected_model = (model or status.get("stt_model") or "").strip()
        if not selected_model:
            return {
                "success": False,
                "transcript": "",
                "error": "No AllModels STT model is selected. Run /speech stt model.",
                "provider": self.name,
            }

        from openai import OpenAI

        client = OpenAI(
            api_key=key, base_url=OPENAI_BASE_URL, timeout=60, max_retries=0
        )
        started = time.monotonic()
        logger.info(
            "AllModels STT path: synchronous file upload (model=%s)",
            selected_model,
        )
        try:
            kwargs: Dict[str, Any] = {
                "model": selected_model,
                "response_format": "json",
            }
            if language:
                kwargs["language"] = language
            prompt = status.get("stt_prompt")
            if isinstance(prompt, str) and prompt.strip():
                kwargs["prompt"] = prompt.strip()
            with open(file_path, "rb") as audio_file:
                kwargs["file"] = audio_file
                response = client.audio.transcriptions.create(**kwargs)

            if isinstance(response, str):
                transcript = response
            elif isinstance(response, dict):
                transcript = str(response.get("text") or "")
            else:
                transcript = str(getattr(response, "text", "") or "")
            logger.info(
                "AllModels STT synchronous complete "
                "(model=%s, transcript_chars=%d, duration=%.2fs)",
                selected_model,
                len(transcript),
                time.monotonic() - started,
            )
            return {"success": True, "transcript": transcript, "provider": self.name}
        except Exception as exc:
            logger.warning(
                "AllModels STT synchronous failed "
                "(model=%s, duration=%.2fs, error_type=%s)",
                selected_model,
                time.monotonic() - started,
                exc.__class__.__name__,
            )
            return {
                "success": False,
                "transcript": "",
                "error": f"AllModels transcription failed: {_safe_exception(exc, key)}",
                "provider": self.name,
            }
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()
