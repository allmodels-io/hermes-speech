"""Profile-scoped, stale-while-revalidate AllModels catalog cache."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from .client import AllModelsAPIError, AllModelsClient

_CACHE_VERSION = 1


def compatible_voices(catalog: Dict[str, Any], model: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return catalog-ordered voices accepted by a synchronous model binding."""
    groups = catalog.get("voices") if isinstance(catalog.get("voices"), dict) else {}
    canonical = str(model.get("id") or "")
    aliases = [str(value) for value in model.get("aliases", []) if isinstance(value, str)]
    result: list[Dict[str, Any]] = []
    seen = set()
    for binding in model.get("providers", []):
        if not isinstance(binding, dict) or binding.get("synchronous") is not True:
            continue
        provider = str(binding.get("id") or "")
        provider_model = str(binding.get("modelId") or "")
        group = groups.get(provider)
        voices = group.get("voices") if isinstance(group, dict) else None
        for voice in voices if isinstance(voices, list) else []:
            if not isinstance(voice, dict) or not voice.get("id"):
                continue
            allowed = voice.get("models")
            accepted_ids = {canonical, provider_model, f"{provider}/{provider_model}", *aliases}
            if isinstance(allowed, list) and allowed and not any(
                str(value) in accepted_ids for value in allowed
            ):
                continue
            voice_id = str(voice["id"])
            dedupe = (provider, voice_id)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            name = str(voice.get("name") or voice_id)
            details = [provider]
            if voice.get("language"):
                details.append(str(voice["language"]))
            if voice.get("gender"):
                details.append(str(voice["gender"]))
            result.append(
                {
                    "id": voice_id,
                    "name": name,
                    "provider": provider,
                    "language": voice.get("language"),
                    "gender": voice.get("gender"),
                    "models": list(allowed) if isinstance(allowed, list) else [],
                    "description": voice.get("description"),
                    "display": f"{name} — {', '.join(details)}",
                }
            )
    return result


def voice_display_name(
    catalog: Optional[Dict[str, Any]],
    voice_id: str,
    provider: str = "",
) -> Optional[str]:
    """Resolve a stored voice ID to its human-readable catalog name."""
    if not catalog or not voice_id:
        return None
    groups = catalog.get("voices")
    if not isinstance(groups, dict):
        return None
    candidates = [provider] if provider and provider in groups else list(groups)
    for group_name in candidates:
        group = groups.get(group_name)
        voices = group.get("voices") if isinstance(group, dict) else None
        for voice in voices if isinstance(voices, list) else []:
            if not isinstance(voice, dict) or str(voice.get("id") or "") != voice_id:
                continue
            name = str(voice.get("name") or "").strip()
            return name or None
    return None


@dataclass
class _CatalogState:
    path: Path
    lock: threading.RLock = field(default_factory=threading.RLock)
    loaded: bool = False
    catalog: Optional[Dict[str, Any]] = None
    refresh_thread: Optional[threading.Thread] = None
    last_error: Optional[AllModelsAPIError] = None


class CatalogStore:
    """Keep independent catalogs for every active Hermes profile."""

    def __init__(self, client: AllModelsClient) -> None:
        self.client = client
        self._states_lock = threading.RLock()
        self._states: Dict[str, _CatalogState] = {}

    @staticmethod
    def _cache_path() -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "cache" / "hermes-speech" / "catalog.json"

    def _state(self) -> _CatalogState:
        path = self._cache_path()
        key = str(path)
        with self._states_lock:
            state = self._states.get(key)
            if state is None:
                state = _CatalogState(path=path)
                self._states[key] = state
        self._load_once(state)
        return state

    def _load_once(self, state: _CatalogState) -> None:
        with state.lock:
            if state.loaded:
                return
            state.loaded = True
            try:
                payload = json.loads(state.path.read_text(encoding="utf-8"))
                if self._valid_cache(payload):
                    state.catalog = payload
            except (OSError, ValueError, TypeError):
                state.catalog = None

    @staticmethod
    def _valid_cache(payload: Any) -> bool:
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            return False
        models = payload.get("models")
        voices = payload.get("voices")
        return (
            isinstance(models, dict)
            and isinstance(models.get("tts"), list)
            and isinstance(models.get("stt"), list)
            and isinstance(voices, dict)
        )

    @staticmethod
    def _validate_live(models: Any, voices: Any) -> Dict[str, Any]:
        if not isinstance(models, dict):
            raise AllModelsAPIError("AllModels returned an invalid model catalog.")
        if not isinstance(models.get("tts"), list) or not isinstance(models.get("stt"), list):
            raise AllModelsAPIError("AllModels returned an invalid model catalog.")
        voice_groups = voices.get("voices") if isinstance(voices, dict) else None
        if not isinstance(voice_groups, dict):
            raise AllModelsAPIError("AllModels returned an invalid voice catalog.")
        return {
            "version": _CACHE_VERSION,
            "fetched_at": time.time(),
            "models": {"tts": models["tts"], "stt": models["stt"]},
            "voices": voice_groups,
        }

    @staticmethod
    def _write_cache(path: Path, catalog: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="catalog-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(catalog, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _refresh(self, state: _CatalogState, api_key: str) -> None:
        try:
            models = self.client.list_models(api_key)
            voices = self.client.list_voices(api_key)
            catalog = self._validate_live(models, voices)
            self._write_cache(state.path, catalog)
            with state.lock:
                state.catalog = catalog
                state.last_error = None
        except AllModelsAPIError as exc:
            with state.lock:
                state.last_error = exc
        except Exception:
            with state.lock:
                state.last_error = AllModelsAPIError(
                    "AllModels catalog refresh failed; the last saved catalog will be used."
                )
        finally:
            with state.lock:
                state.refresh_thread = None

    def _start_refresh(self, state: _CatalogState, api_key: str) -> threading.Thread:
        with state.lock:
            running = state.refresh_thread
            if running is not None and running.is_alive():
                return running
            thread = threading.Thread(
                target=self._refresh,
                args=(state, api_key),
                name="hermes-speech-catalog",
                daemon=True,
            )
            state.refresh_thread = thread
            thread.start()
            return thread

    def ensure(self, api_key: str, *, cold_timeout: float = 2.0) -> Optional[Dict[str, Any]]:
        """Return cached data immediately and refresh it in the background."""
        state = self._state()
        with state.lock:
            cached = state.catalog
        thread = self._start_refresh(state, api_key)
        if cached is None:
            thread.join(max(0.0, cold_timeout))
        with state.lock:
            return state.catalog

    def cached(self) -> Optional[Dict[str, Any]]:
        state = self._state()
        with state.lock:
            return state.catalog

    def last_error(self) -> Optional[AllModelsAPIError]:
        state = self._state()
        with state.lock:
            return state.last_error

    def clear_error(self) -> None:
        state = self._state()
        with state.lock:
            state.last_error = None

    def replace_for_tests(self, catalog: Dict[str, Any]) -> None:
        state = self._state()
        with state.lock:
            state.catalog = catalog
            state.loaded = True
            state.last_error = None
