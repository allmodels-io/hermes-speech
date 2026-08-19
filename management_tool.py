"""Agent-facing management tool for an existing AllModels speech account."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Sequence

from . import settings
from .catalog import CatalogStore, voice_display_name
from .client import AllModelsAPIError, AllModelsClient
from .providers import AllModelsTTSProvider, _eligible_models
from .update_checker import PluginUpdateChecker

_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_RESULT_LIMIT = 10


MANAGEMENT_TOOL_SCHEMA = {
    "name": "allmodels_speech_manage",
    "description": (
        "Manage an existing AllModels speech account and Hermes TTS/STT configuration. "
        "Use for status, model discovery and selection, compatible voice search and "
        "selection, non-mutating one-off voice previews, balance, top-up links, configured "
        "TTS tests, speed, language, transcription prompts, and plugin update checks or "
        "installation. This tool cannot sign up or "
        "verify accounts; use allmodels_speech_setup for first-time setup. Load "
        "manage-allmodels-speech with skill_view for the workflow."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "get_status",
                    "list_model_authors",
                    "find_models",
                    "select_model",
                    "find_voices",
                    "select_voice",
                    "get_balance",
                    "create_topup_link",
                    "preview_voice",
                    "test_tts",
                    "set_speed",
                    "set_language",
                    "set_prompt",
                    "check_update",
                    "update_plugin",
                ],
                "description": (
                    "The speech-management operation to perform. update_plugin requires an "
                    "explicit user request and does not require an AllModels account."
                ),
            },
            "capability": {
                "type": "string",
                "enum": ["tts", "stt"],
                "description": "Speech capability for model actions.",
            },
            "author": {
                "type": "string",
                "description": "Exact model author used to filter find_models.",
            },
            "query": {
                "type": "string",
                "description": "Partial model or voice search text. Results are returned; no partial match is auto-selected.",
            },
            "model_id": {
                "type": "string",
                "description": "Exact canonical model ID or alias for select_model, model-scoped find_voices, or preview_voice.",
            },
            "voice_id": {
                "type": "string",
                "description": "Exact compatible voice ID returned by find_voices.",
            },
            "voice_provider": {
                "type": "string",
                "description": "Voice provider returned by find_voices; required only if an ID is ambiguous.",
            },
            "amount_usd": {
                "type": "number",
                "minimum": 5,
                "maximum": 1000,
                "description": "Top-up amount in USD with at most two decimal places.",
            },
            "text": {
                "type": "string",
                "description": "Text to synthesize for preview_voice or test_tts, or transcription prompt for set_prompt.",
            },
            "speed": {
                "type": "number",
                "minimum": 0.25,
                "maximum": 4,
                "description": "TTS speed multiplier for set_speed.",
            },
            "language": {
                "type": "string",
                "description": "BCP-47-like language tag for set_language, such as en, ja, or pt-BR.",
            },
            "use_default": {
                "type": "boolean",
                "description": "Remove the speed or language override and restore inherited/default behavior.",
            },
            "clear": {
                "type": "boolean",
                "description": "Clear the configured STT prompt for set_prompt.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _result(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _public_voice(voice: Dict[str, Any]) -> Dict[str, Any]:
    result = {
        key: voice.get(key)
        for key in (
            "id",
            "name",
            "provider",
            "provider_name",
            "language",
            "gender",
            "description",
            "category",
            "preview_url",
            "labels",
            "metrics",
        )
        if voice.get(key) not in (None, "")
    }
    result["compatible_models"] = list(voice.get("models") or [])
    return result


def _model_search_text(model: Dict[str, Any], query: str) -> bool:
    needle = query.lower()
    values = [str(model.get("id") or "")]
    aliases = model.get("aliases")
    if isinstance(aliases, list):
        values.extend(str(alias) for alias in aliases)
    return all(term in " ".join(values).lower() for term in needle.split())


def _exact_model(
    models: Sequence[Dict[str, Any]], model_id: str
) -> Optional[Dict[str, Any]]:
    needle = model_id.strip().lower()
    for model in models:
        if str(model.get("id") or "").lower() == needle:
            return model
        aliases = model.get("aliases")
        if isinstance(aliases, list) and any(
            str(alias).lower() == needle for alias in aliases
        ):
            return model
    return None


class AllModelsSpeechManagementTool:
    """Structured management operations backed by the native plugin services."""

    def __init__(
        self,
        client: AllModelsClient,
        catalog: CatalogStore,
        tts_provider: AllModelsTTSProvider,
        update_checker: Optional[PluginUpdateChecker] = None,
    ) -> None:
        self.client = client
        self.catalog = catalog
        self.tts_provider = tts_provider
        self.update_checker = update_checker

    def handle(self, args: dict, **_: Any) -> str:
        action = str(args.get("action") or "get_status").strip().lower()
        if action in {"check_update", "update_plugin"}:
            if self.update_checker is None:
                return _result(success=False, error="update_support_unavailable")
            result = (
                self.update_checker.check_now()
                if action == "check_update"
                else self.update_checker.update_now()
            )
            return _result(**result)

        result = self._handle(args)
        if self.update_checker is not None:
            return self.update_checker.decorate_json(result)
        return result

    def _handle(self, args: dict) -> str:
        action = str(args.get("action") or "get_status").strip().lower()
        if not self.client.get_api_key():
            return _result(
                success=False,
                authenticated=False,
                error="account_required",
                next_action="load_configure_allmodels_speech",
                instruction=(
                    "Load configure-allmodels-speech and use allmodels_speech_setup. "
                    "Do not attempt signup with this management tool."
                ),
            )
        try:
            handlers = {
                "get_status": self._get_status,
                "list_model_authors": self._list_model_authors,
                "find_models": self._find_models,
                "select_model": self._select_model,
                "find_voices": self._find_voices,
                "select_voice": self._select_voice,
                "get_balance": self._get_balance,
                "create_topup_link": self._create_topup_link,
                "preview_voice": self._preview_voice,
                "test_tts": self._test_tts,
                "set_speed": self._set_speed,
                "set_language": self._set_language,
                "set_prompt": self._set_prompt,
            }
            handler = handlers.get(action)
            if handler is None:
                return _result(success=False, error="unsupported_action")
            return handler(args)
        except AllModelsAPIError as exc:
            return _result(
                success=False,
                error=str(exc),
                error_code=exc.code or None,
                authentication_error=exc.is_auth_error,
            )
        except RuntimeError as exc:
            return _result(success=False, error=str(exc))

    def _catalog(self) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        catalog = self.catalog.ensure(self.client.get_api_key(), cold_timeout=2.0)
        error = self.catalog.last_error()
        if error is not None and error.is_auth_error:
            return None, "saved_credentials_rejected"
        if catalog is None:
            return None, "catalog_initializing"
        return catalog, None

    def _catalog_or_error(self) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
        catalog, error = self._catalog()
        if error:
            return None, _result(
                success=False,
                error=error,
                retryable=error == "catalog_initializing",
            )
        return catalog, None

    @staticmethod
    def _capability(args: Dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
        capability = str(args.get("capability") or "").strip().lower()
        if capability not in {"tts", "stt"}:
            return None, _result(
                success=False,
                error="capability_required",
                valid_capabilities=["tts", "stt"],
            )
        return capability, None

    def _get_status(self, _args: Dict[str, Any]) -> str:
        catalog, catalog_error = self._catalog()
        status = settings.speech_status()
        if catalog_error == "saved_credentials_rejected":
            return _result(
                success=False,
                authenticated=False,
                error="saved_credentials_rejected",
                configured=bool(
                    status.get("tts_provider") == "allmodels"
                    or status.get("stt_provider") == "allmodels"
                ),
                instruction=(
                    "Ask whether the user wants to replace the account, then load "
                    "configure-allmodels-speech. Do not request an API key."
                ),
            )
        voice_id = str(status.get("tts_voice") or "")
        provider = str(status.get("tts_voice_provider") or "")
        voice_name = (
            voice_display_name(catalog, voice_id, provider) if catalog else None
        )
        if not voice_name and voice_id and status.get("tts_model"):
            resolved = self.catalog.find_voice(
                str(status["tts_model"]), voice_id, provider
            )
            if resolved is not None:
                voice_name = str(resolved.get("name") or "") or None
        return _result(
            success=True,
            authenticated=True,
            catalog_status="available" if catalog else catalog_error,
            tts={
                "active": status.get("tts_provider") == "allmodels",
                "model": status.get("tts_model"),
                "voice": voice_name or ("configured voice" if voice_id else None),
                "voice_id": voice_id or None,
                "voice_provider": provider or None,
                "speed": status.get("tts_speed"),
                "output_format": status.get("tts_output_format") or "mp3",
            },
            stt={
                "active": bool(
                    status.get("stt_enabled")
                    and status.get("stt_provider") == "allmodels"
                ),
                "model": status.get("stt_model"),
                "language_override": status.get("stt_language"),
                "inherited_language": status.get("stt_inherited_language"),
                "prompt_configured": bool(status.get("stt_prompt")),
            },
        )

    def _list_model_authors(self, args: Dict[str, Any]) -> str:
        capability, error = self._capability(args)
        if error:
            return error
        catalog, error = self._catalog_or_error()
        if error:
            return error
        assert catalog is not None and capability is not None
        authors: list[str] = []
        seen: set[str] = set()
        for model in _eligible_models(catalog, capability):
            author = str(model.get("id") or "").partition("/")[0]
            if author and author.lower() not in seen:
                seen.add(author.lower())
                authors.append(author)
        return _result(
            success=True,
            capability=capability,
            authors=authors[:_RESULT_LIMIT],
            result_limit=_RESULT_LIMIT,
            more_available=len(authors) > _RESULT_LIMIT,
        )

    def _find_models(self, args: Dict[str, Any]) -> str:
        capability, error = self._capability(args)
        if error:
            return error
        catalog, error = self._catalog_or_error()
        if error:
            return error
        assert catalog is not None and capability is not None
        author = str(args.get("author") or "").strip()
        query = str(args.get("query") or "").strip()
        models = _eligible_models(catalog, capability)
        if author:
            models = [
                model
                for model in models
                if str(model.get("id") or "").partition("/")[0].lower()
                == author.lower()
            ]
        if query:
            models = [model for model in models if _model_search_text(model, query)]
        results = []
        for model in models[:_RESULT_LIMIT]:
            model_id = str(model.get("id") or "")
            item: Dict[str, Any] = {
                "id": model_id,
                "author": model_id.partition("/")[0],
            }
            aliases = model.get("aliases")
            if isinstance(aliases, list) and aliases:
                item["aliases"] = [str(alias) for alias in aliases]
            for key in ("name", "description"):
                if model.get(key):
                    item[key] = model[key]
            results.append(item)
        return _result(
            success=True,
            capability=capability,
            author=author or None,
            query=query or None,
            models=results,
            result_limit=_RESULT_LIMIT,
            more_available=len(models) > _RESULT_LIMIT,
            instruction=(
                "Use the exact id or alias with select_model. Never select a partial match."
            ),
        )

    def _select_model(self, args: Dict[str, Any]) -> str:
        capability, error = self._capability(args)
        if error:
            return error
        model_id = str(args.get("model_id") or "").strip()
        if not model_id:
            return _result(success=False, error="model_id_required")
        catalog, error = self._catalog_or_error()
        if error:
            return error
        assert catalog is not None and capability is not None
        model = _exact_model(_eligible_models(catalog, capability), model_id)
        if model is None:
            return _result(
                success=False,
                error="model_not_found",
                instruction="Call find_models and select an exact canonical ID or alias.",
            )
        canonical = str(model.get("id") or "")
        if capability == "stt":
            settings.set_stt_model(canonical)
            return _result(
                success=True,
                capability="stt",
                selected_model=canonical,
                language_behavior="Hermes inherited/default language resolution",
            )

        status = settings.speech_status()
        current_voice = str(status.get("tts_voice") or "")
        current_provider = str(status.get("tts_voice_provider") or "")
        page = self.catalog.search_voices(model_id=canonical, page_size=_RESULT_LIMIT)
        voices = page["voices"]
        keep_voice = bool(
            current_voice
            and self.catalog.find_voice(canonical, current_voice, current_provider)
        )
        cleared = bool(current_voice and not keep_voice)
        settings.set_tts_model(canonical, clear_voice=cleared)
        return _result(
            success=True,
            capability="tts",
            selected_model=canonical,
            previous_voice_cleared=cleared,
            compatible_voices=[
                _public_voice(voice) for voice in voices[:_RESULT_LIMIT]
            ],
            more_voices_available=page["has_more"],
            next_action="select_voice" if voices else None,
        )

    def _active_model_and_voices(
        self,
    ) -> tuple[Optional[str], Optional[list[Dict[str, Any]]], Optional[str]]:
        model_id = str(settings.speech_status().get("tts_model") or "")
        if not model_id:
            return (
                None,
                None,
                _result(
                    success=False,
                    error="tts_model_required",
                    instruction="Select a TTS model before searching or selecting voices.",
                ),
            )
        return self._model_and_voices(model_id)

    def _model_and_voices(
        self, model_id: str
    ) -> tuple[Optional[str], Optional[list[Dict[str, Any]]], Optional[str]]:
        catalog, error = self._catalog_or_error()
        if error:
            return None, None, error
        assert catalog is not None
        model = _exact_model(_eligible_models(catalog, "tts"), model_id)
        if model is None:
            return (
                None,
                None,
                _result(
                    success=False,
                    error="selected_tts_model_unavailable",
                    instruction="Choose a current TTS model with find_models and select_model.",
                ),
            )
        canonical = str(model.get("id") or "")
        page = self.catalog.search_voices(model_id=canonical, page_size=_RESULT_LIMIT)
        return canonical, page["voices"], None

    def _find_voices(self, args: Dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        requested_model = str(args.get("model_id") or "").strip()
        scope = "all_models" if query and not requested_model else "model"
        more_available = False
        if scope == "all_models":
            # The API returns one row per model/voice/provider combination.
            # Fetch enough ranked rows to present ten distinct voices rather
            # than ten copies of the same voice across compatible models.
            page = self.catalog.search_voices(query=query, page_size=100)
            merged: Dict[tuple[str, str], Dict[str, Any]] = {}
            for voice in page["voices"]:
                key = (str(voice.get("provider") or ""), str(voice.get("id") or ""))
                existing = merged.get(key)
                if existing is None:
                    existing = dict(voice)
                    existing["compatible_models"] = []
                    merged[key] = existing
                compatible_model = str(voice.get("model") or "")
                if (
                    compatible_model
                    and compatible_model not in existing["compatible_models"]
                ):
                    existing["compatible_models"].append(compatible_model)
            matches = list(merged.values())
            more_available = page["has_more"] or len(matches) > _RESULT_LIMIT
            model_id = None
        else:
            if requested_model:
                model_id, voices, error = self._model_and_voices(requested_model)
            else:
                model_id, voices, error = self._active_model_and_voices()
            if error:
                return error
            assert voices is not None
            if query:
                page = self.catalog.search_voices(
                    model_id=model_id or "",
                    query=query,
                    page_size=_RESULT_LIMIT,
                )
                matches = page["voices"]
                more_available = page["has_more"]
            else:
                matches = voices
                more_available = len(voices) >= _RESULT_LIMIT

        results = []
        for voice in matches[:_RESULT_LIMIT]:
            item = _public_voice(voice)
            compatible_models = voice.get("compatible_models")
            if isinstance(compatible_models, list):
                item["compatible_models"] = compatible_models
            elif model_id:
                item["compatible_models"] = [model_id]
            results.append(item)
        return _result(
            success=True,
            model=model_id,
            scope=scope,
            query=query or None,
            voices=results,
            result_limit=_RESULT_LIMIT,
            more_available=more_available,
            instruction=(
                "Use select_voice to change the configured voice, or preview_voice with an "
                "exact compatible model, voice id, and provider to listen without changing configuration."
            ),
        )

    def _select_voice(self, args: Dict[str, Any]) -> str:
        model_id, voices, error = self._active_model_and_voices()
        if error:
            return error
        assert voices is not None
        voice_id = str(args.get("voice_id") or "").strip()
        provider = str(args.get("voice_provider") or "").strip()
        if not voice_id:
            return _result(success=False, error="voice_id_required")
        selected = self.catalog.find_voice(model_id or "", voice_id, provider)
        matches = [selected] if selected is not None else []
        if not matches:
            return _result(
                success=False,
                error="incompatible_voice",
                model=model_id,
                valid_voices=[_public_voice(voice) for voice in voices[:_RESULT_LIMIT]],
                instruction="Call find_voices and select an exact compatible voice.",
            )
        if len(matches) > 1:
            return _result(
                success=False,
                error="ambiguous_voice_id",
                matches=[_public_voice(voice) for voice in matches],
                instruction="Repeat select_voice with voice_provider.",
            )
        selected = matches[0]
        settings.set_tts_voice(str(selected["id"]), str(selected["provider"]))
        return _result(
            success=True,
            model=model_id,
            selected_voice={
                "name": selected.get("name") or selected["id"],
                "id": selected["id"],
                "provider": selected["provider"],
            },
        )

    def _get_balance(self, _args: Dict[str, Any]) -> str:
        data = self.client.get_balance()
        try:
            paid = Decimal(str(data.get("paid_balance_usd", 0)))
        except InvalidOperation:
            paid = Decimal("0")
        try:
            promotional = Decimal(str(data.get("promotional_credits", 0))) / Decimal(
                "1000000"
            )
        except InvalidOperation:
            promotional = Decimal("0")
        grants = []
        for grant in data.get("promotion_grants", []):
            if not isinstance(grant, dict):
                continue
            grants.append(
                {
                    "name": grant.get("name") or "Grant",
                    "remaining_usd": str(grant.get("remaining_usd", 0)),
                    "eligible": bool(grant.get("eligible")),
                    "expires_at": grant.get("expires_at"),
                }
            )
        return _result(
            success=True,
            state=data.get("state", "unknown"),
            spendable_paid_balance_usd=f"{paid:.2f}",
            promotional_balance_usd=f"{promotional:.2f}",
            promotional_grants=grants[:_RESULT_LIMIT],
        )

    def _create_topup_link(self, args: Dict[str, Any]) -> str:
        raw = args.get("amount_usd")
        if raw is None:
            return _result(
                success=False,
                error="amount_required",
                minimum_usd="5.00",
                maximum_usd="1000.00",
            )
        try:
            amount = Decimal(str(raw))
        except InvalidOperation:
            return _result(success=False, error="invalid_amount")
        if (
            not amount.is_finite()
            or amount.as_tuple().exponent < -2
            or amount < Decimal("5")
            or amount > Decimal("1000")
        ):
            return _result(
                success=False,
                error="invalid_amount",
                instruction="Use $5-$1,000 with at most two decimal places.",
            )
        data = self.client.create_topup(amount)
        url = str(data.get("url") or "")
        if not url.startswith("https://"):
            return _result(success=False, error="invalid_topup_link")
        return _result(
            success=True,
            amount_usd=f"{amount:.2f}",
            url=url,
            expires_at=data.get("expires_at"),
            instruction="Return the secure link without opening it. No charge occurs until the user completes payment.",
        )

    def _preview_voice(self, args: Dict[str, Any]) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return _result(success=False, error="text_required")
        requested_model = str(args.get("model_id") or "").strip()
        if not requested_model:
            return _result(
                success=False,
                error="model_id_required",
                instruction="Use an exact compatible model returned by find_voices.",
            )
        model_id, voices, error = self._model_and_voices(requested_model)
        if error:
            return error
        assert model_id is not None and voices is not None
        voice_id = str(args.get("voice_id") or "").strip()
        provider = str(args.get("voice_provider") or "").strip()
        if not voice_id:
            return _result(success=False, error="voice_id_required")
        resolved = self.catalog.find_voice(model_id, voice_id, provider)
        matches = [resolved] if resolved is not None else []
        if not matches:
            return _result(
                success=False,
                error="incompatible_voice",
                model=model_id,
                valid_voices=[_public_voice(voice) for voice in voices[:_RESULT_LIMIT]],
            )
        if len(matches) > 1:
            return _result(
                success=False,
                error="ambiguous_voice_id",
                matches=[_public_voice(voice) for voice in matches],
                instruction="Repeat preview_voice with voice_provider.",
            )
        speed_value = args.get("speed")
        speed: Optional[float] = None
        if speed_value is not None:
            try:
                speed = float(speed_value)
            except (TypeError, ValueError):
                return _result(
                    success=False, error="invalid_speed", minimum=0.25, maximum=4
                )
            if not 0.25 <= speed <= 4:
                return _result(
                    success=False, error="invalid_speed", minimum=0.25, maximum=4
                )

        selected = matches[0]
        from hermes_constants import get_hermes_home

        output = get_hermes_home() / "media" / f"speech-preview-{uuid.uuid4().hex}.mp3"
        try:
            path = self.tts_provider.synthesize(
                text,
                str(output),
                model=model_id,
                voice=str(selected["id"]),
                voice_provider=str(selected["provider"]),
                speed=speed,
                format="mp3",
            )
        except Exception as exc:
            return _result(
                success=False, error=str(exc), error_code="voice_preview_failed"
            )
        media_path = f'"{path}"' if " " in path else path
        return _result(
            success=True,
            preview={
                "model": model_id,
                "voice": selected.get("name") or selected["id"],
                "voice_id": selected["id"],
                "voice_provider": selected["provider"],
                "speed": speed,
            },
            configuration_changed=False,
            media_path=path,
            media_directive=f"[[audio_as_voice]]\nMEDIA:{media_path}",
            instruction=(
                "Include media_directive exactly in the response. This was a one-off preview; "
                "do not claim the configured model or voice changed."
            ),
        )

    def _test_tts(self, args: Dict[str, Any]) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return _result(success=False, error="text_required")
        status = settings.speech_status()
        if not status.get("tts_model") or not status.get("tts_voice"):
            return _result(success=False, error="tts_setup_incomplete")
        from hermes_constants import get_hermes_home

        output = get_hermes_home() / "media" / f"speech-test-{uuid.uuid4().hex}.mp3"
        try:
            path = self.tts_provider.synthesize(
                text,
                str(output),
                model=str(status["tts_model"]),
                voice=str(status["tts_voice"]),
                speed=(
                    float(status["tts_speed"])
                    if status.get("tts_speed") is not None
                    else None
                ),
                format="mp3",
            )
        except Exception as exc:
            return _result(success=False, error=str(exc), error_code="tts_test_failed")
        media_path = f'"{path}"' if " " in path else path
        return _result(
            success=True,
            media_path=path,
            media_directive=f"[[audio_as_voice]]\nMEDIA:{media_path}",
            instruction="Include media_directive exactly in the response so Hermes delivers the audio.",
        )

    def _set_speed(self, args: Dict[str, Any]) -> str:
        if args.get("use_default") is True:
            settings.set_tts_speed(None)
            return _result(success=True, speed=None, behavior="default")
        raw = args.get("speed")
        try:
            speed = float(raw)
        except (TypeError, ValueError):
            return _result(
                success=False, error="speed_required", minimum=0.25, maximum=4
            )
        if not 0.25 <= speed <= 4:
            return _result(
                success=False, error="invalid_speed", minimum=0.25, maximum=4
            )
        settings.set_tts_speed(speed)
        return _result(success=True, speed=speed)

    def _set_language(self, args: Dict[str, Any]) -> str:
        if args.get("use_default") is True:
            settings.set_stt_language(None)
            inherited = settings.speech_status().get("stt_inherited_language")
            return _result(
                success=True,
                language_override=None,
                inherited_language=inherited,
                behavior="inherited" if inherited else "automatic_detection",
            )
        language = str(args.get("language") or "").strip()
        if not _LANGUAGE_RE.fullmatch(language):
            return _result(
                success=False,
                error="invalid_language",
                examples=["en", "ja", "pt-BR"],
            )
        settings.set_stt_language(language)
        return _result(success=True, language_override=language)

    def _set_prompt(self, args: Dict[str, Any]) -> str:
        if args.get("clear") is True:
            settings.set_stt_prompt(None)
            return _result(success=True, prompt_configured=False)
        prompt = str(args.get("text") or "").strip()
        if not prompt:
            return _result(success=False, error="prompt_required")
        settings.set_stt_prompt(prompt)
        return _result(success=True, prompt_configured=True)
