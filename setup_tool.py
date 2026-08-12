"""Agent-facing conversational setup tool for AllModels speech."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from . import settings
from .catalog import CatalogStore, compatible_voices, voice_display_name
from .client import AllModelsAPIError, AllModelsClient
from .providers import _eligible_models
from .update_checker import PluginUpdateChecker

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CODE_RE = re.compile(r"^\d{6}$")
_PREFERRED_TTS = ("fish/s2-1-pro",)
_PREFERRED_STT = ("soniox/stt-async-v5",)
_PREFERRED_VOICES = {
    "fish/s2-1-pro": ("fish", "03397b4c4be74759b72533b663fbd001"),
}


SETUP_TOOL_SCHEMA = {
    "name": "allmodels_speech_setup",
    "description": (
        "Set up AllModels as Hermes' native TTS and STT provider. Use this when the user "
        "asks to set up, configure, connect, or enable speech without naming another provider, "
        "or explicitly asks for AllModels. Do not configure Edge/local speech or install packages. Start with status; "
        "if no account exists, request their email and start signup. When an already-authorized "
        "email search integration is available, retrieve the newest verification code from "
        "noreply@allmodels.io; otherwise ask the user for it. Verification automatically installs balanced TTS/STT defaults. "
        "Load configure-allmodels-speech with skill_view for the full workflow."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "start_signup", "verify_signup", "configure_defaults"],
                "description": "The next setup operation to perform.",
            },
            "email": {
                "type": "string",
                "description": "Account email, required for start_signup and verify_signup.",
            },
            "code": {
                "type": "string",
                "description": "Six-digit email code, required for verify_signup.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _result(**payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _mask_email(email: str) -> str:
    local, _, domain = email.partition("@")
    if len(local) <= 2:
        masked = local[:1] + "*"
    else:
        masked = local[0] + "*" * min(5, len(local) - 2) + local[-1]
    return f"{masked}@{domain}"


def _preferred_model(models: list[Dict[str, Any]], preferred: tuple[str, ...]) -> Optional[Dict[str, Any]]:
    by_id = {str(model.get("id") or ""): model for model in models}
    for model_id in preferred:
        if model_id in by_id:
            return by_id[model_id]
    return models[0] if models else None


class AllModelsSpeechSetupTool:
    """State-light setup workflow suitable for every Hermes surface."""

    def __init__(
        self,
        client: AllModelsClient,
        catalog: CatalogStore,
        update_checker: Optional[PluginUpdateChecker] = None,
    ) -> None:
        self.client = client
        self.catalog = catalog
        self.update_checker = update_checker

    def handle(self, args: dict, **_: Any) -> str:
        result = self._handle(args)
        if self.update_checker is not None:
            return self.update_checker.decorate_json(result)
        return result

    def _handle(self, args: dict) -> str:
        action = str(args.get("action") or "status").strip().lower()
        try:
            if action == "status":
                return self._status()
            if action == "start_signup":
                return self._start_signup(str(args.get("email") or ""))
            if action == "verify_signup":
                return self._verify_signup(
                    str(args.get("email") or ""),
                    str(args.get("code") or ""),
                )
            if action == "configure_defaults":
                return self._configure_defaults()
            return _result(success=False, error="unsupported_action", next_action="status")
        except (AllModelsAPIError, RuntimeError) as exc:
            return _result(success=False, error=str(exc), next_action="status")

    def _status(self) -> str:
        key = self.client.get_api_key()
        status = settings.speech_status()
        configured = bool(
            status.get("tts_provider") == "allmodels"
            and status.get("tts_model")
            and status.get("tts_voice")
            and status.get("stt_provider") == "allmodels"
            and status.get("stt_model")
        )
        if not key:
            return _result(
                success=True,
                authenticated=False,
                configured=False,
                next_action="start_signup",
                needs="email",
                workflow_skill="configure-allmodels-speech",
                instruction=(
                    "Stop setup work and ask only for the user's email. Do not configure "
                    "Edge/local speech and do not install packages."
                ),
            )

        catalog = self.catalog.ensure(key, cold_timeout=2.0)
        error = self.catalog.last_error()
        if error is not None and error.is_auth_error:
            return _result(
                success=False,
                authenticated=False,
                configured=configured,
                error="saved_credentials_rejected",
                recovery=(
                    "Ask whether the user wants to replace the saved AllModels account, then "
                    "use start_signup with their email."
                ),
                next_action="start_signup",
                needs="confirmation_and_email",
            )
        return _result(
            success=True,
            authenticated=True,
            configured=configured,
            selections=self._public_status(status, catalog),
            next_action="done" if configured else "configure_defaults",
            workflow_skill="configure-allmodels-speech",
            instruction=(
                "Report the selections if setup is done; otherwise call configure_defaults. "
                "Do not configure an alternative speech provider."
            ),
        )

    def _start_signup(self, raw_email: str) -> str:
        email = raw_email.strip().lower()
        if not _EMAIL_RE.fullmatch(email):
            return _result(
                success=False,
                error="invalid_email",
                next_action="start_signup",
                needs="valid_email",
            )
        self.client.preflight_credential_store()
        response = self.client.start_signup(email)
        try:
            expires_in = max(30, min(900, int(response.get("expiresIn", 300))))
        except (TypeError, ValueError):
            expires_in = 300
        return _result(
            success=True,
            signup_started=True,
            email=_mask_email(email),
            expires_in_seconds=expires_in,
            next_action="verify_signup",
            needs="six_digit_code",
            verification_email={
                "sender": "noreply@allmodels.io",
                "subject_contains": "Your Allmodels verification code",
                "subject_example": "675693 — Your Allmodels verification code",
                "code_pattern": "six digits at the start of the subject",
                "prefer_newest_after_signup": True,
            },
            instruction=(
                "If this session already has authorized email search access, find the newest "
                "message from noreply@allmodels.io whose subject contains 'Your Allmodels "
                "verification code' (for example, '675693 — Your Allmodels verification "
                "code') and use the leading six-digit code. Do not require an exact subject "
                "match and do not initiate email authorization. If "
                "email access is unavailable or no matching recent message is found, stop and "
                "ask only for the code."
            ),
        )

    def _verify_signup(self, raw_email: str, raw_code: str) -> str:
        email = raw_email.strip().lower()
        code = raw_code.strip()
        if not _EMAIL_RE.fullmatch(email):
            return _result(success=False, error="invalid_email", next_action="verify_signup")
        if not _CODE_RE.fullmatch(code):
            return _result(
                success=False,
                error="invalid_code_format",
                next_action="verify_signup",
                needs="six_digit_code",
            )
        self.client.preflight_credential_store()
        response = self.client.verify_signup(email, code)
        api_key = str(response.get("apiKey") or response.get("api_key") or "").strip()
        self.client.save_api_key(api_key)
        self.catalog.clear_error()
        configured = json.loads(self._configure_defaults(api_key=api_key))
        configured["account_created"] = True
        configured["credentials_saved"] = True
        return json.dumps(configured, ensure_ascii=False, sort_keys=True)

    def _configure_defaults(self, *, api_key: str = "") -> str:
        key = (api_key or self.client.get_api_key()).strip()
        if not key:
            return _result(
                success=False,
                authenticated=False,
                error="account_required",
                next_action="start_signup",
                needs="email",
            )
        catalog = self.catalog.ensure(key, cold_timeout=2.0)
        if catalog is None:
            return _result(
                success=False,
                authenticated=True,
                error="catalog_initializing",
                next_action="configure_defaults",
                retryable=True,
            )
        error = self.catalog.last_error()
        if error is not None and error.is_auth_error:
            return _result(
                success=False,
                authenticated=False,
                error="saved_credentials_rejected",
                next_action="start_signup",
                needs="confirmation_and_email",
            )

        tts_models = _eligible_models(catalog, "tts")
        stt_models = _eligible_models(catalog, "stt")
        tts_model = _preferred_model(tts_models, _PREFERRED_TTS)
        stt_model = _preferred_model(stt_models, _PREFERRED_STT)
        if tts_model is None or stt_model is None:
            return _result(
                success=False,
                error="no_eligible_speech_models",
                next_action="configure_defaults",
                retryable=True,
            )
        voices = compatible_voices(catalog, tts_model)
        voice = self._default_voice(catalog, tts_model, voices)
        if voice is None:
            return _result(
                success=False,
                error="no_compatible_tts_voice",
                model=tts_model.get("id"),
                next_action="configure_defaults",
                retryable=True,
            )

        settings.configure_defaults(
            tts_model=str(tts_model["id"]),
            tts_voice=str(voice["id"]),
            tts_voice_provider=str(voice["provider"]),
            stt_model=str(stt_model["id"]),
        )
        return _result(
            success=True,
            authenticated=True,
            configured=True,
            preset="balanced",
            selections=self._public_status(settings.speech_status(), catalog),
            next_action="done",
            voice_mode_hint="Use /voice on and /voice tts to enable normal Hermes spoken replies.",
        )

    @staticmethod
    def _default_voice(
        catalog: Dict[str, Any],
        model: Dict[str, Any],
        voices: list[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        preferred = _PREFERRED_VOICES.get(str(model.get("id") or ""))
        if preferred is not None:
            preferred_provider, preferred_id = preferred
            match = next(
                (
                    voice
                    for voice in voices
                    if voice["provider"] == preferred_provider and voice["id"] == preferred_id
                ),
                None,
            )
            if match is not None:
                return match

        groups = catalog.get("voices") if isinstance(catalog.get("voices"), dict) else {}
        for binding in model.get("providers", []):
            if not isinstance(binding, dict) or binding.get("synchronous") is not True:
                continue
            provider = str(binding.get("id") or "")
            group = groups.get(provider)
            default_id = str(group.get("default") or "") if isinstance(group, dict) else ""
            match = next(
                (
                    voice
                    for voice in voices
                    if voice["provider"] == provider and voice["id"] == default_id
                ),
                None,
            )
            if match is not None:
                return match
        return voices[0] if voices else None

    @staticmethod
    def _public_status(
        status: Dict[str, Any],
        catalog: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        provider = str(status.get("tts_voice_provider") or "")
        voice_id = str(status.get("tts_voice") or "")
        voice_name = voice_display_name(catalog, voice_id, provider)
        if not voice_name and voice_id:
            voice_name = f"Configured {provider.title() or 'TTS'} voice"
        return {
            "tts_model": status.get("tts_model"),
            "tts_voice": voice_name,
            "tts_voice_provider": provider or None,
            "stt_model": status.get("stt_model"),
        }
