"""Guided ``/speech`` command for the Hermes Speech plugin."""

from __future__ import annotations

import re
import shlex
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import settings
from .catalog import CatalogStore, voice_display_name
from .client import AllModelsAPIError, AllModelsClient
from .providers import AllModelsTTSProvider, _eligible_models
from .update_checker import PluginUpdateChecker

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")
_MENU_TTL_SECONDS = 30 * 60


@dataclass
class _PendingSignup:
    email: str
    expires_at: float


@dataclass
class _MenuSnapshot:
    items: List[Dict[str, Any]]
    expires_at: float
    meta: Dict[str, Any]


class SpeechCommand:
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
        self._state_lock = threading.RLock()
        self._pending_signups: Dict[Tuple[str, ...], _PendingSignup] = {}
        self._menus: Dict[Tuple[Tuple[str, ...], str], _MenuSnapshot] = {}
        self._last_model_stage: Dict[Tuple[Tuple[str, ...], str], str] = {}

    @staticmethod
    def _identity() -> Tuple[str, ...]:
        from gateway.session_context import get_session_env
        from hermes_constants import get_hermes_home

        home = str(get_hermes_home())
        session = (
            get_session_env("HERMES_SESSION_KEY")
            or get_session_env("HERMES_SESSION_ID")
            or get_session_env("HERMES_UI_SESSION_ID")
            or "local"
        )
        user = (
            get_session_env("HERMES_SESSION_USER_ID")
            or get_session_env("HERMES_SESSION_USER_NAME")
            or "local"
        )
        platform = (
            get_session_env("HERMES_SESSION_PLATFORM")
            or get_session_env("HERMES_SESSION_SOURCE")
            or "cli"
        )
        return home, platform, session, user

    @staticmethod
    def _split(raw_args: str) -> List[str]:
        try:
            return shlex.split(raw_args)
        except ValueError:
            return raw_args.split()

    def _clean_expired(self, identity: Tuple[str, ...]) -> None:
        now = time.monotonic()
        with self._state_lock:
            pending = self._pending_signups.get(identity)
            if pending and pending.expires_at <= now:
                self._pending_signups.pop(identity, None)
            for key, snapshot in list(self._menus.items()):
                if key[0] == identity and snapshot.expires_at <= now:
                    self._menus.pop(key, None)

    def _save_menu(
        self,
        identity: Tuple[str, ...],
        stage: str,
        items: Sequence[Dict[str, Any]],
        **meta: Any,
    ) -> None:
        with self._state_lock:
            self._menus[(identity, stage)] = _MenuSnapshot(
                items=[dict(item) for item in items],
                expires_at=time.monotonic() + _MENU_TTL_SECONDS,
                meta=dict(meta),
            )
            if stage in {"tts_authors", "tts_models"}:
                self._last_model_stage[(identity, "tts")] = stage
            elif stage in {"stt_authors", "stt_models"}:
                self._last_model_stage[(identity, "stt")] = stage

    def _menu(self, identity: Tuple[str, ...], stage: str) -> Optional[_MenuSnapshot]:
        self._clean_expired(identity)
        with self._state_lock:
            return self._menus.get((identity, stage))

    @staticmethod
    def _numbered(title: str, items: Sequence[Dict[str, Any]], footer: str) -> str:
        lines = [title, ""]
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. {item.get('display') or item.get('id')}")
        lines.extend(["", footer])
        return "\n".join(lines)

    def handle(self, raw_args: str) -> str:
        parts = self._split(raw_args.strip())
        if parts and parts[0].lower() in {"update", "6"}:
            if self.update_checker is None:
                return "Hermes Speech update support is unavailable in this process. Restart Hermes."
            if len(parts) > 1 and parts[1].lower() == "check":
                return self.update_checker.format_check()
            if len(parts) > 1:
                return "Use `/speech update` to install, or `/speech update check` to check only."
            return self.update_checker.format_update()

        result = self._handle(raw_args)
        if self.update_checker is not None:
            return self.update_checker.decorate_text(result)
        return result

    def _handle(self, raw_args: str) -> str:
        identity = self._identity()
        self._clean_expired(identity)
        parts = self._split(raw_args.strip())
        key = self.client.get_api_key()

        if not key:
            return self._handle_unauthenticated(identity, parts)

        # Every authenticated invocation refreshes in the background. Cached
        # data remains immediately available; a cold cache waits at most 2s.
        catalog = self.catalog.ensure(key, cold_timeout=2.0)
        error = self.catalog.last_error()
        if (
            error is not None
            and error.is_auth_error
            and (not parts or parts[0].lower() not in {"signup", "verify"})
        ):
            return (
                "The saved AllModels API key was rejected. Update ALLMODELS_API_KEY through "
                "Hermes credential setup, or run `/speech signup <email>` to replace it."
            )

        if not parts:
            return self._control_center()

        parts = self._expand_numeric_route(parts)
        command = parts[0].lower()
        rest = parts[1:]

        if command == "status":
            return self._control_center()
        if command == "signup":
            return self._start_signup(identity, rest)
        if command == "verify":
            return self._verify_signup(identity, rest)
        if command == "tts":
            return self._handle_tts(identity, rest, catalog)
        if command == "stt":
            return self._handle_stt(identity, rest, catalog)
        if command == "account":
            return self._handle_account(identity, rest)
        if command == "balance":
            return self._balance()
        if command == "topup":
            return self._topup(rest)
        if command == "test":
            return self._test_tts(" ".join(rest))
        if command == "advanced":
            return self._advanced(rest)
        return self._control_center(error=f"Unknown option: {parts[0]}")

    def _handle_unauthenticated(
        self, identity: Tuple[str, ...], parts: List[str]
    ) -> str:
        if parts:
            command = parts[0].lower()
            if command == "signup":
                return self._start_signup(identity, parts[1:])
            if command == "verify":
                return self._verify_signup(identity, parts[1:])
            if len(parts) == 1 and _EMAIL_RE.match(parts[0]):
                return self._start_signup(identity, parts)
        pending = self._pending(identity)
        if pending:
            return (
                f"A verification code was sent to {self._mask_email(pending.email)}.\n\n"
                "Finish signup with `/speech verify 123456`."
            )
        return (
            "Welcome to Hermes Speech. An AllModels account is required.\n\n"
            "Start secure email signup with:\n"
            "`/speech signup you@example.com`"
        )

    def _pending(self, identity: Tuple[str, ...]) -> Optional[_PendingSignup]:
        self._clean_expired(identity)
        with self._state_lock:
            return self._pending_signups.get(identity)

    @staticmethod
    def _mask_email(email: str) -> str:
        local, _, domain = email.partition("@")
        if len(local) <= 2:
            masked = local[:1] + "*"
        else:
            masked = local[0] + "*" * min(5, len(local) - 2) + local[-1]
        return f"{masked}@{domain}"

    def _start_signup(self, identity: Tuple[str, ...], args: Sequence[str]) -> str:
        if not args:
            return "Enter the account email with `/speech signup you@example.com`."
        email = args[0].strip().lower()
        if not _EMAIL_RE.match(email):
            return "That does not look like a valid email address. Use `/speech signup you@example.com`."
        try:
            self.client.preflight_credential_store()
            result = self.client.start_signup(email)
        except (AllModelsAPIError, RuntimeError) as exc:
            return str(exc)
        expires = result.get("expiresIn", 300)
        try:
            ttl = max(30, min(900, int(expires)))
        except (TypeError, ValueError):
            ttl = 300
        with self._state_lock:
            self._pending_signups[identity] = _PendingSignup(
                email, time.monotonic() + ttl
            )
        return (
            f"AllModels sent a six-digit code to {self._mask_email(email)}. "
            f"It expires in about {max(1, ttl // 60)} minute(s).\n\n"
            "Verify with `/speech verify 123456`."
        )

    def _verify_signup(self, identity: Tuple[str, ...], args: Sequence[str]) -> str:
        pending = self._pending(identity)
        if pending is None:
            return "No active signup was found. Start again with `/speech signup you@example.com`."
        if not args or not re.fullmatch(r"\d{6}", args[0]):
            return "Enter the six-digit code with `/speech verify 123456`."
        try:
            self.client.preflight_credential_store()
            result = self.client.verify_signup(pending.email, args[0])
            api_key = str(result.get("apiKey") or "").strip()
            self.client.save_api_key(api_key)
        except (AllModelsAPIError, RuntimeError) as exc:
            return str(exc)
        with self._state_lock:
            self._pending_signups.pop(identity, None)
        self.catalog.clear_error()
        catalog = self.catalog.ensure(api_key, cold_timeout=2.0)
        if catalog is None:
            return (
                "AllModels signup is complete and the API key was saved securely. "
                "The speech catalog is initializing; run `/speech tts model` in a moment."
            )
        return "AllModels signup is complete.\n\n" + self._show_authors(
            identity, "tts", catalog
        )

    def _control_center(self, *, error: str = "") -> str:
        status = settings.speech_status()
        voice = self._selected_voice_display(status)
        lines: List[str] = []
        if error:
            lines.extend([error, ""])
        lines.extend(
            [
                "Hermes Speech — AllModels",
                "",
                f"TTS: {status.get('tts_model') or 'not configured'}",
                f"Voice: {voice}",
                f"STT: {status.get('stt_model') or 'not configured'}",
                "",
                "1. TTS setup — `/speech tts`",
                "2. STT setup — `/speech stt`",
                "3. Account — `/speech account`",
                "4. Test TTS — `/speech test <text>`",
                "5. Advanced — `/speech advanced`",
                "6. Update plugin — `/speech update`",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _expand_numeric_route(parts: List[str]) -> List[str]:
        root = {
            "1": "tts",
            "2": "stt",
            "3": "account",
            "4": "test",
            "5": "advanced",
            "6": "update",
        }
        result = list(parts)
        result[0] = root.get(result[0], result[0])
        if len(result) > 1:
            nested = {
                "tts": {"1": "model", "2": "voice"},
                "stt": {"1": "model"},
                "account": {"1": "signup", "2": "balance", "3": "topup"},
                "advanced": {"1": "speed", "2": "language", "3": "prompt"},
            }
            result[1] = nested.get(result[0].lower(), {}).get(result[1], result[1])
        return result

    def _handle_tts(
        self,
        identity: Tuple[str, ...],
        args: Sequence[str],
        catalog: Optional[Dict[str, Any]],
    ) -> str:
        if not args:
            status = settings.speech_status()
            voice = self._selected_voice_display(status)
            return (
                f"TTS model: {status.get('tts_model') or 'not configured'}\n"
                f"Voice: {voice}\n\n"
                "1. Choose model — `/speech tts model`\n"
                "2. Choose voice — `/speech tts voice`"
            )
        action = args[0].lower()
        if action == "model":
            return self._model_flow(identity, "tts", list(args[1:]), catalog)
        if action == "voice":
            return self._voice_flow(identity, list(args[1:]), catalog)
        return "Use `/speech tts model` or `/speech tts voice`."

    def _selected_voice_display(self, status: Dict[str, Any]) -> str:
        voice_id = str(status.get("tts_voice") or "")
        if not voice_id:
            return "not configured"
        provider = str(status.get("tts_voice_provider") or "")
        name = voice_display_name(self.catalog.cached(), voice_id, provider)
        if not name and status.get("tts_model"):
            try:
                resolved = self.catalog.find_voice(
                    str(status["tts_model"]), voice_id, provider
                )
                if resolved is not None:
                    name = str(resolved.get("name") or "") or None
            except AllModelsAPIError:
                pass
        return name or f"Configured {provider.title() or 'TTS'} voice"

    def _handle_stt(
        self,
        identity: Tuple[str, ...],
        args: Sequence[str],
        catalog: Optional[Dict[str, Any]],
    ) -> str:
        if not args:
            status = settings.speech_status()
            return (
                f"STT model: {status.get('stt_model') or 'not configured'}\n\n"
                "1. Choose model — `/speech stt model`"
            )
        if args[0].lower() == "model":
            return self._model_flow(identity, "stt", list(args[1:]), catalog)
        return "Use `/speech stt model`."

    def _model_flow(
        self,
        identity: Tuple[str, ...],
        capability: str,
        args: List[str],
        catalog: Optional[Dict[str, Any]],
    ) -> str:
        if catalog is None:
            return "The AllModels speech catalog is initializing. Try this command again in a moment."
        models = _eligible_models(catalog, capability)
        if not models:
            return f"AllModels currently advertises no synchronous {capability.upper()} models."
        if not args:
            return self._show_authors(identity, capability, catalog)

        query = " ".join(args).strip()
        if query.isdigit():
            stage = self._last_model_stage.get((identity, capability))
            snapshot = self._menu(identity, stage) if stage else None
            if snapshot:
                selected = self._number_selection(snapshot, query)
                if selected is None:
                    return f"Choose a number from 1 to {len(snapshot.items)}."
                if stage.endswith("authors"):
                    return self._show_models(
                        identity, capability, catalog, selected["id"]
                    )
                return self._select_model(identity, capability, catalog, selected)

        exact_model = self._exact_model(models, query)
        if exact_model is not None:
            return self._select_model(identity, capability, catalog, exact_model)

        authors = self._authors(models)
        exact_author = next(
            (author for author in authors if author.lower() == query.lower()), None
        )
        if exact_author:
            return self._show_models(identity, capability, catalog, exact_author)

        author_matches = [
            author for author in authors if query.lower() in author.lower()
        ]
        if author_matches:
            return self._show_authors(identity, capability, catalog, query=query)

        matches = [model for model in models if self._model_search_text(model, query)]
        if not matches:
            return f"No {capability.upper()} authors or models matched {query!r}."
        return self._show_model_items(
            identity,
            capability,
            matches[:10],
            title=f"Matching {capability.upper()} models",
        )

    @staticmethod
    def _authors(models: Sequence[Dict[str, Any]]) -> List[str]:
        seen = set()
        result = []
        for model in models:
            author = str(model.get("id") or "").partition("/")[0]
            key = author.lower()
            if author and key not in seen:
                seen.add(key)
                result.append(author)
        return result

    def _show_authors(
        self,
        identity: Tuple[str, ...],
        capability: str,
        catalog: Dict[str, Any],
        *,
        query: str = "",
    ) -> str:
        authors = self._authors(_eligible_models(catalog, capability))
        if query:
            authors = [author for author in authors if query.lower() in author.lower()]
        items = [{"id": author, "display": author} for author in authors[:10]]
        if not items:
            return f"No {capability.upper()} model authors matched {query!r}."
        stage = f"{capability}_authors"
        self._save_menu(identity, stage, items)
        return self._numbered(
            f"Choose a {capability.upper()} model author",
            items,
            f"Select with `/speech {capability} model <number-or-author>`.",
        )

    def _show_models(
        self,
        identity: Tuple[str, ...],
        capability: str,
        catalog: Dict[str, Any],
        author: str,
    ) -> str:
        models = [
            model
            for model in _eligible_models(catalog, capability)
            if str(model.get("id") or "").partition("/")[0].lower() == author.lower()
        ]
        return self._show_model_items(
            identity,
            capability,
            models[:10],
            title=f"Choose a {capability.upper()} model by {author}",
            author=author,
        )

    def _show_model_items(
        self,
        identity: Tuple[str, ...],
        capability: str,
        models: Sequence[Dict[str, Any]],
        *,
        title: str,
        author: str = "",
    ) -> str:
        items = [dict(model, display=model.get("id")) for model in models]
        if not items:
            return f"No synchronous {capability.upper()} models are available for that author."
        stage = f"{capability}_models"
        self._save_menu(identity, stage, items, author=author)
        return self._numbered(
            title,
            items,
            f"Select with `/speech {capability} model <number-or-full-id>`.",
        )

    @staticmethod
    def _model_search_text(model: Dict[str, Any], query: str) -> bool:
        needle = query.lower()
        values = [str(model.get("id") or "")]
        aliases = model.get("aliases")
        if isinstance(aliases, list):
            values.extend(str(alias) for alias in aliases)
        return any(needle in value.lower() for value in values)

    @staticmethod
    def _exact_model(
        models: Sequence[Dict[str, Any]], query: str
    ) -> Optional[Dict[str, Any]]:
        needle = query.lower()
        for model in models:
            if str(model.get("id") or "").lower() == needle:
                return model
            aliases = model.get("aliases")
            if isinstance(aliases, list) and any(
                str(alias).lower() == needle for alias in aliases
            ):
                return model
        return None

    @staticmethod
    def _number_selection(
        snapshot: _MenuSnapshot, raw: str
    ) -> Optional[Dict[str, Any]]:
        try:
            index = int(raw) - 1
        except ValueError:
            return None
        if 0 <= index < len(snapshot.items):
            return snapshot.items[index]
        return None

    def _select_model(
        self,
        identity: Tuple[str, ...],
        capability: str,
        catalog: Dict[str, Any],
        model: Dict[str, Any],
    ) -> str:
        model_id = str(model.get("id") or "")
        if capability == "stt":
            settings.set_stt_model(model_id)
            return (
                f"STT model set to {model_id}. Hermes will use its existing language default.\n\n"
                "Language and prompt overrides are available under `/speech advanced`."
            )

        status = settings.speech_status()
        current_voice = str(status.get("tts_voice") or "")
        current_provider = str(status.get("tts_voice_provider") or "")
        try:
            page = self.catalog.search_voices(model_id=model_id, page_size=10)
            compatible = page["voices"]
            keep_voice = bool(
                current_voice
                and self.catalog.find_voice(model_id, current_voice, current_provider)
            )
        except AllModelsAPIError as exc:
            return str(exc)
        settings.set_tts_model(
            model_id, clear_voice=bool(current_voice and not keep_voice)
        )
        heading = f"TTS model set to {model_id}."
        if current_voice and not keep_voice:
            heading += " The previous voice was incompatible and has been cleared."
        return heading + "\n\n" + self._show_voice_items(identity, compatible)

    def _voice_flow(
        self,
        identity: Tuple[str, ...],
        args: List[str],
        catalog: Optional[Dict[str, Any]],
    ) -> str:
        if catalog is None:
            return "The AllModels voice catalog is initializing. Try this command again in a moment."
        status = settings.speech_status()
        model_id = str(status.get("tts_model") or "")
        if not model_id:
            return "Choose a TTS model first with `/speech tts model`."
        model = self._exact_model(_eligible_models(catalog, "tts"), model_id)
        if model is None:
            return "The selected TTS model is no longer in the catalog. Choose a new model."
        if not args:
            try:
                voices = self.catalog.search_voices(model_id=model_id, page_size=10)[
                    "voices"
                ]
            except AllModelsAPIError as exc:
                return str(exc)
            return self._show_voice_items(identity, voices)
        explicit_search = args[0].lower() == "search"
        if explicit_search:
            args = args[1:]
            if not args:
                return (
                    "Enter a voice name, ID, language, gender, or provider with "
                    "`/speech tts voice search <query>`."
                )
        query = " ".join(args).strip()
        if query.isdigit():
            snapshot = self._menu(identity, "tts_voices")
            if snapshot:
                selected = self._number_selection(snapshot, query)
                if selected is None:
                    return f"Choose a number from 1 to {len(snapshot.items)}."
                return self._select_voice(selected)
        try:
            voices = self.catalog.search_voices(
                model_id=model_id,
                query=query,
                page_size=10,
            )["voices"]
        except AllModelsAPIError as exc:
            return str(exc)
        exact = [voice for voice in voices if voice["id"].lower() == query.lower()]
        if len(exact) == 1:
            return self._select_voice(exact[0])
        if not voices:
            return f"No compatible voices matched {query!r}."
        return self._show_voice_items(identity, voices)

    def _show_voice_items(
        self, identity: Tuple[str, ...], voices: Sequence[Dict[str, Any]]
    ) -> str:
        items = [dict(voice) for voice in voices[:10]]
        if not items:
            return "No compatible voices are currently advertised for this model."
        self._save_menu(identity, "tts_voices", items)
        return self._numbered(
            "Choose a compatible voice",
            items,
            "Search with `/speech tts voice search <query>`, or select with "
            "`/speech tts voice <number-or-voice-id>`.",
        )

    @staticmethod
    def _select_voice(voice: Dict[str, Any]) -> str:
        settings.set_tts_voice(str(voice["id"]), str(voice["provider"]))
        return (
            f"TTS voice set to {voice.get('name') or voice['id']} via {voice['provider']}.\n\n"
            "TTS setup is complete. Optional speed tuning is under `/speech advanced`."
        )

    def _handle_account(self, identity: Tuple[str, ...], args: Sequence[str]) -> str:
        if not args:
            return (
                "AllModels account\n\n"
                "1. Start signup or replace account — `/speech signup <email>`\n"
                "2. Balance — `/speech balance`\n"
                "3. Add balance — `/speech topup <usd>`"
            )
        action = args[0].lower()
        if action == "signup":
            return self._start_signup(identity, args[1:])
        if action == "balance":
            return self._balance()
        if action == "topup":
            return self._topup(args[1:])
        return "Use `/speech signup`, `/speech balance`, or `/speech topup <usd>`."

    def _balance(self) -> str:
        try:
            data = self.client.get_balance()
        except AllModelsAPIError as exc:
            return str(exc)
        paid = data.get("paid_balance_usd", 0)
        promotional = data.get("promotional_credits", 0)
        try:
            promotional_usd = Decimal(str(promotional)) / Decimal("1000000")
        except InvalidOperation:
            promotional_usd = Decimal("0")
        lines = [
            "AllModels balance",
            "",
            f"Status: {data.get('state', 'unknown')}",
            f"Spendable paid balance: ${Decimal(str(paid)):.2f}",
            f"Promotional balance: ${promotional_usd:.2f}",
        ]
        grants = data.get("promotion_grants")
        if isinstance(grants, list) and grants:
            lines.extend(["", "Promotional grants:"])
            for grant in grants[:10]:
                if not isinstance(grant, dict):
                    continue
                eligible = (
                    "eligible" if grant.get("eligible") else "not currently eligible"
                )
                expiry = grant.get("expires_at") or "no expiry"
                lines.append(
                    f"- {grant.get('name', 'Grant')}: ${Decimal(str(grant.get('remaining_usd', 0))):.2f}, "
                    f"{eligible}, expires {expiry}"
                )
        return "\n".join(lines)

    def _topup(self, args: Sequence[str]) -> str:
        if not args:
            return "Choose an amount from $5 to $1,000: `/speech topup 25`."
        raw = args[0].strip().lstrip("$")
        try:
            amount = Decimal(raw)
        except InvalidOperation:
            return "Top-up amount must be a number from $5 to $1,000."
        if (
            amount.as_tuple().exponent < -2
            or amount < Decimal("5")
            or amount > Decimal("1000")
        ):
            return "Top-up amount must be from $5 to $1,000 with at most two decimal places."
        try:
            data = self.client.create_topup(amount)
        except AllModelsAPIError as exc:
            return str(exc)
        url = str(data.get("url") or "")
        if not url.startswith("https://"):
            return "AllModels created the top-up request but did not return a valid secure payment link."
        expires = data.get("expires_at")
        suffix = f"\nExpires: {expires}" if expires else ""
        return f"Secure AllModels top-up link for ${amount:.2f}:\n{url}{suffix}"

    def _test_tts(self, text: str) -> str:
        if not text.strip():
            return "Provide test text: `/speech test Hello from Hermes`."
        status = settings.speech_status()
        if not status.get("tts_model") or not status.get("tts_voice"):
            return "Complete TTS model and voice setup before running a test."
        from hermes_constants import get_hermes_home

        output = get_hermes_home() / "media" / f"speech-test-{uuid.uuid4().hex}.mp3"
        try:
            path = self.tts_provider.synthesize(
                text.strip(),
                str(output),
                model=str(status["tts_model"]),
                voice=str(status["tts_voice"]),
                speed=float(status["tts_speed"])
                if status.get("tts_speed") is not None
                else None,
                format="mp3",
            )
        except Exception as exc:
            return str(exc)
        media_path = f'"{path}"' if " " in path else path
        return f"TTS test generated.\n[[audio_as_voice]]\nMEDIA:{media_path}"

    def _advanced(self, args: Sequence[str]) -> str:
        status = settings.speech_status()
        if not args:
            inherited = status.get("stt_inherited_language") or "automatic detection"
            return (
                "Advanced speech tuning\n\n"
                f"1. TTS speed: {status.get('tts_speed') if status.get('tts_speed') is not None else 'default'}\n"
                f"2. STT language: {status.get('stt_language') or f'default ({inherited})'}\n"
                f"3. STT prompt: {'configured' if status.get('stt_prompt') else 'not configured'}\n\n"
                "Use `/speech advanced speed`, `/speech advanced language`, or `/speech advanced prompt`."
            )
        action = args[0].lower()
        values = list(args[1:])
        if action == "speed":
            if not values:
                return "Set `0.25`–`4`, or restore the default with `/speech advanced speed default`."
            if values[0].lower() == "default":
                settings.set_tts_speed(None)
                return "TTS speed restored to the default."
            try:
                speed = float(values[0])
            except ValueError:
                return "TTS speed must be from 0.25 to 4, or `default`."
            if not 0.25 <= speed <= 4:
                return "TTS speed must be from 0.25 to 4."
            settings.set_tts_speed(speed)
            return f"TTS speed set to {speed:g}×."
        if action == "language":
            if not values:
                return "Set a language tag such as `en` or `ja`, or use `default`."
            language = values[0]
            if language.lower() == "default":
                settings.set_stt_language(None)
                inherited = (
                    settings.speech_status().get("stt_inherited_language")
                    or "automatic detection"
                )
                return f"STT language restored to the Hermes default ({inherited})."
            if not _LANGUAGE_RE.fullmatch(language):
                return "Use a language tag such as `en`, `ja`, or `pt-BR`."
            settings.set_stt_language(language)
            return f"AllModels STT language set to {language}."
        if action == "prompt":
            if not values:
                return "Set a transcription hint, or clear it with `/speech advanced prompt clear`."
            prompt = " ".join(values).strip()
            if prompt.lower() == "clear":
                settings.set_stt_prompt(None)
                return "The AllModels STT prompt was cleared."
            settings.set_stt_prompt(prompt)
            return "The AllModels STT prompt was updated."
        return "Advanced options are `speed`, `language`, and `prompt`."
