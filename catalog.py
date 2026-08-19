"""Profile-scoped model cache and searchable AllModels voice catalogue."""

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

_CACHE_VERSION = 2
_MAX_CACHED_VOICES = 1000


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_voice_entry(entry: Any) -> list[Dict[str, Any]]:
    """Flatten one v1 voice-catalogue row into provider-specific choices."""
    if not isinstance(entry, dict):
        return []
    model = entry.get("model")
    voice = entry.get("voice")
    providers = entry.get("providers")
    if (
        not isinstance(model, dict)
        or not isinstance(voice, dict)
        or not isinstance(providers, list)
    ):
        return []
    model_id = _text(model.get("id"))
    voice_id = _text(voice.get("id"))
    if not model_id or not voice_id:
        return []

    languages = []
    for language in voice.get("languages", []):
        if isinstance(language, dict) and _text(language.get("id")):
            languages.append(
                {
                    key: language.get(key)
                    for key in ("id", "name", "locale", "accent")
                    if language.get(key) not in (None, "")
                }
            )

    labels: Dict[str, list[Dict[str, str]]] = {}
    for label in voice.get("labels", []):
        if not isinstance(label, dict):
            continue
        facet = label.get("facet")
        value = label.get("value")
        if not isinstance(facet, dict) or not isinstance(value, dict):
            continue
        facet_id = _text(facet.get("id"))
        value_id = _text(value.get("id"))
        if facet_id and value_id:
            labels.setdefault(facet_id, []).append(
                {"id": value_id, "name": _text(value.get("name")) or value_id}
            )

    language = _text(languages[0].get("id")) if languages else ""
    if not language and labels.get("language"):
        language = _text(labels["language"][0].get("id"))
    gender = _text(labels.get("gender", [{}])[0].get("id"))
    if not gender:
        descriptors = {item["id"] for item in labels.get("descriptor", [])}
        gender = next(
            (
                value
                for value in ("female", "male", "nonbinary")
                if value in descriptors
            ),
            "",
        )

    category = voice.get("category")
    category_id = _text(category.get("id")) if isinstance(category, dict) else ""
    name = _text(voice.get("name")) or voice_id
    result = []
    for provider in providers:
        if not isinstance(provider, dict):
            continue
        provider_id = _text(provider.get("id"))
        if not provider_id:
            continue
        details = [provider_id]
        if language:
            details.append(language)
        if gender:
            details.append(gender)
        result.append(
            {
                "id": voice_id,
                "name": name,
                "provider": provider_id,
                "provider_name": _text(provider.get("name")) or provider_id,
                "provider_model_id": _text(provider.get("provider_model_id")),
                "provider_default": provider.get("default") is True,
                "model": model_id,
                "models": [model_id],
                "language": language or None,
                "languages": languages,
                "gender": gender or None,
                "labels": labels,
                "category": category_id or None,
                "description": voice.get("description"),
                "preview_url": voice.get("preview_url"),
                "metrics": provider.get("metrics"),
                "first_seen_at": voice.get("first_seen_at"),
                "last_seen_at": voice.get("last_seen_at"),
                "display": f"{name} — {', '.join(details)}",
            }
        )
    return result


def _voice_search_text(voice: Dict[str, Any], query: str) -> bool:
    values: list[Any] = [
        voice.get("id"),
        voice.get("name"),
        voice.get("provider"),
        voice.get("provider_name"),
        voice.get("model"),
        voice.get("language"),
        voice.get("gender"),
        voice.get("category"),
        voice.get("description"),
        voice.get("keywords"),
    ]
    for language in voice.get("languages", []):
        if isinstance(language, dict):
            values.extend(language.values())
    labels = voice.get("labels")
    if isinstance(labels, dict):
        values.extend(labels)
        for entries in labels.values():
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        values.extend(entry.values())
    haystack = " ".join(str(value) for value in values if value).lower()
    return all(term in haystack for term in query.lower().split())


def _cacheable_voice(voice: Dict[str, Any]) -> Dict[str, Any]:
    """Keep only compact display/fallback data, never the full API row."""
    cached = {
        key: voice.get(key)
        for key in (
            "id",
            "name",
            "provider",
            "provider_name",
            "model",
            "models",
            "language",
            "gender",
            "category",
            "description",
            "display",
        )
        if voice.get(key) not in (None, "", [], {})
    }
    keywords: list[str] = []
    for language in voice.get("languages", []):
        if isinstance(language, dict):
            keywords.extend(str(value) for value in language.values() if value)
    labels = voice.get("labels")
    if isinstance(labels, dict):
        for facet, entries in labels.items():
            keywords.append(str(facet))
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        keywords.extend(str(value) for value in entry.values() if value)
    if keywords:
        cached["keywords"] = sorted(set(keywords), key=str.lower)
    return cached


def voice_display_name(
    catalog: Optional[Dict[str, Any]], voice_id: str, provider: str = ""
) -> Optional[str]:
    """Resolve a stored voice ID from the profile's queried-voice cache."""
    if not catalog or not voice_id:
        return None
    entries = catalog.get("voice_entries")
    for voice in entries if isinstance(entries, list) else []:
        if not isinstance(voice, dict) or _text(voice.get("id")) != voice_id:
            continue
        if provider and _text(voice.get("provider")) != provider:
            continue
        return _text(voice.get("name")) or None
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
    """Keep models and previously queried rich voice records per Hermes profile."""

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
                    payload["voice_entries"] = [
                        _cacheable_voice(voice)
                        for voice in payload["voice_entries"]
                        if isinstance(voice, dict)
                    ][-_MAX_CACHED_VOICES:]
                    state.catalog = payload
            except (OSError, ValueError, TypeError):
                state.catalog = None

    @staticmethod
    def _valid_cache(payload: Any) -> bool:
        if not isinstance(payload, dict) or payload.get("version") != _CACHE_VERSION:
            return False
        models = payload.get("models")
        return (
            isinstance(models, dict)
            and isinstance(models.get("tts"), list)
            and isinstance(models.get("stt"), list)
            and isinstance(payload.get("voice_entries"), list)
        )

    @staticmethod
    def _validate_models(models: Any) -> Dict[str, Any]:
        if (
            not isinstance(models, dict)
            or not isinstance(models.get("tts"), list)
            or not isinstance(models.get("stt"), list)
        ):
            raise AllModelsAPIError("AllModels returned an invalid model catalog.")
        return {"tts": models["tts"], "stt": models["stt"]}

    @staticmethod
    def _validate_voice_page(
        payload: Any,
    ) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
        rows = payload.get("voices") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise AllModelsAPIError("AllModels returned an invalid voice catalogue.")
        voices = [voice for row in rows for voice in _normalize_voice_entry(row)]
        return voices, {
            "total_count": int(payload.get("total_count") or 0),
            "has_more": payload.get("has_more") is True,
            "next_cursor": payload.get("next_cursor"),
            "catalogue_updated_at": payload.get("catalogue_updated_at"),
            "facets": payload.get("facets")
            if isinstance(payload.get("facets"), list)
            else [],
        }

    @staticmethod
    def _write_cache(path: Path, catalog: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix="catalog-", suffix=".tmp", dir=str(path.parent)
        )
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
            models = self._validate_models(self.client.list_models(api_key))
            with state.lock:
                previous = state.catalog or {}
                catalog = {
                    "version": _CACHE_VERSION,
                    "fetched_at": time.time(),
                    "models": models,
                    "voice_entries": [
                        _cacheable_voice(voice)
                        for voice in previous.get("voice_entries", [])
                        if isinstance(voice, dict)
                    ][-_MAX_CACHED_VOICES:],
                    "voice_catalogue_updated_at": previous.get(
                        "voice_catalogue_updated_at"
                    ),
                }
                self._write_cache(state.path, catalog)
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

    def ensure(
        self, api_key: str, *, cold_timeout: float = 2.0
    ) -> Optional[Dict[str, Any]]:
        """Return cached models immediately and refresh them in the background."""
        state = self._state()
        with state.lock:
            cached = state.catalog
        thread = self._start_refresh(state, api_key)
        if cached is None:
            thread.join(max(0.0, cold_timeout))
        with state.lock:
            return state.catalog

    def search_voices(
        self,
        *,
        model_id: str = "",
        query: str = "",
        provider: str = "",
        language: str = "",
        category: str = "",
        label: str = "",
        page_size: int = 10,
        include_facets: bool = False,
    ) -> Dict[str, Any]:
        """Search live and retain rich results for offline display/fallback."""
        state = self._state()
        try:
            payload = self.client.list_voices(
                query=query,
                model=model_id,
                provider=provider,
                language=language,
                category=category,
                label=label,
                sort="relevance" if query else "featured",
                page_size=page_size,
                include_facets=include_facets,
            )
            voices, metadata = self._validate_voice_page(payload)
            with state.lock:
                catalog = state.catalog
                if catalog is not None:
                    existing = {
                        (
                            _text(item.get("model")),
                            _text(item.get("provider")),
                            _text(item.get("id")),
                        ): item
                        for item in catalog.get("voice_entries", [])
                        if isinstance(item, dict)
                    }
                    for voice in voices:
                        key = (voice["model"], voice["provider"], voice["id"])
                        existing.pop(key, None)
                        existing[key] = _cacheable_voice(voice)
                    catalog["voice_entries"] = list(existing.values())[
                        -_MAX_CACHED_VOICES:
                    ]
                    catalog["voice_catalogue_updated_at"] = metadata[
                        "catalogue_updated_at"
                    ]
                    try:
                        self._write_cache(state.path, catalog)
                    except OSError:
                        pass
                state.last_error = None
            return {"voices": voices, "stale": False, **metadata}
        except AllModelsAPIError as exc:
            with state.lock:
                state.last_error = exc
                catalog = state.catalog or {}
                cached = [
                    dict(voice)
                    for voice in catalog.get("voice_entries", [])
                    if isinstance(voice, dict)
                    and (not model_id or _text(voice.get("model")) == model_id)
                    and (not provider or _text(voice.get("provider")) == provider)
                    and (not query or _voice_search_text(voice, query))
                ]
            if not cached:
                raise
            return {
                "voices": cached[:page_size],
                "total_count": len(cached),
                "has_more": len(cached) > page_size,
                "next_cursor": None,
                "catalogue_updated_at": catalog.get("voice_catalogue_updated_at"),
                "facets": [],
                "stale": True,
            }

    def find_voice(
        self, model_id: str, voice_id: str, provider: str = ""
    ) -> Optional[Dict[str, Any]]:
        """Resolve and validate an exact voice choice for a model."""
        page = self.search_voices(model_id=model_id, query=voice_id, page_size=100)
        matches = [
            voice
            for voice in page["voices"]
            if _text(voice.get("id")).lower() == voice_id.lower()
            and (
                not provider or _text(voice.get("provider")).lower() == provider.lower()
            )
        ]
        return matches[0] if len(matches) == 1 else None

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
