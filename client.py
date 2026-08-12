"""AllModels HTTP and credential client used by the Hermes Speech plugin."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import httpx

API_ROOT = "https://api.allmodels.io"
OPENAI_BASE_URL = f"{API_ROOT}/oai"
API_KEY_ENV = "ALLMODELS_API_KEY"

_ERROR_MESSAGES = {
    "invalid_request": "The request was not accepted. Check the supplied value and try again.",
    "invalid_or_expired_code": "That verification code is invalid or expired. Start signup again for a new code.",
    "too_many_attempts": "Too many verification attempts. Start signup again for a new code.",
    "rate_limited": "AllModels is rate-limiting this request. Wait a moment and try again.",
    "email_delivery_failed": "AllModels could not send the verification email. Try again shortly.",
    "account_service_unavailable": "The AllModels account service is temporarily unavailable.",
    "agent_signup_failed": "AllModels could not complete signup. Start the signup flow again.",
    "missing_api_key": "The saved AllModels API key is missing or invalid.",
    "invalid_api_key": "The saved AllModels API key is invalid.",
    "inactive_api_key": "The saved AllModels API key is inactive.",
}


class AllModelsAPIError(RuntimeError):
    """Safe, structured AllModels API failure."""

    def __init__(self, message: str, *, code: str = "", status_code: int = 0) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in {401, 403} or self.code in {
            "missing_api_key",
            "invalid_api_key",
            "inactive_api_key",
        }


def _extract_error(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    raw = payload.get("error")
    if isinstance(raw, str):
        return raw, str(payload.get("message") or "")
    if isinstance(raw, dict):
        return str(raw.get("code") or raw.get("type") or ""), str(raw.get("message") or "")
    return str(payload.get("code") or ""), str(payload.get("message") or "")


class AllModelsClient:
    """Small synchronous client with bounded requests and safe errors."""

    def __init__(self, *, api_root: str = API_ROOT, timeout: float = 15.0) -> None:
        self.api_root = api_root.rstrip("/")
        self.timeout = timeout

    def get_api_key(self) -> str:
        try:
            from hermes_cli.config import get_env_value

            return (get_env_value(API_KEY_ENV) or "").strip()
        except Exception:
            return os.environ.get(API_KEY_ENV, "").strip()

    def preflight_credential_store(self) -> None:
        """Fail before signup when the active profile cannot store a returned key."""
        from hermes_cli.config import get_env_path, is_managed

        if is_managed():
            raise RuntimeError("This Hermes installation is managed and cannot save an AllModels API key.")
        try:
            from hermes_cli import managed_scope

            if managed_scope.is_env_managed(API_KEY_ENV):
                raise RuntimeError("ALLMODELS_API_KEY is managed by your administrator and cannot be replaced.")
        except ImportError:
            pass

        env_path = Path(get_env_path())
        if env_path.exists():
            if not os.access(env_path, os.W_OK):
                raise RuntimeError(f"Hermes cannot write its credential file: {env_path}")
            return

        probe = env_path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        if not os.access(probe, os.W_OK):
            raise RuntimeError(f"Hermes cannot create its credential file under: {env_path.parent}")

    def save_api_key(self, api_key: str) -> None:
        value = (api_key or "").strip()
        if not value:
            raise RuntimeError("AllModels returned an empty API key.")
        from hermes_cli.credential_lifecycle import save_provider_env_credential

        save_provider_env_credential(API_KEY_ENV, value)

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        api_key: str = "",
        authenticated: bool = True,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        request_headers = {"Accept": "application/json"}
        used_key = ""
        if authenticated:
            key = (api_key or self.get_api_key()).strip()
            if not key:
                raise AllModelsAPIError(
                    _ERROR_MESSAGES["missing_api_key"], code="missing_api_key", status_code=401
                )
            request_headers["Authorization"] = f"Bearer {key}"
            used_key = key
        if headers:
            request_headers.update(headers)

        try:
            response = httpx.request(
                method,
                f"{self.api_root}{path}",
                headers=request_headers,
                json=json_body,
                timeout=self.timeout,
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise AllModelsAPIError("The AllModels request timed out. Try again.") from exc
        except httpx.HTTPError as exc:
            raise AllModelsAPIError("Could not connect to AllModels. Check the network and try again.") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.is_success:
            code, detail = _extract_error(payload)
            message = _ERROR_MESSAGES.get(code)
            if not message:
                if response.status_code == 401:
                    message = _ERROR_MESSAGES["invalid_api_key"]
                elif response.status_code == 403:
                    message = _ERROR_MESSAGES["inactive_api_key"]
                elif response.status_code == 429:
                    message = _ERROR_MESSAGES["rate_limited"]
                elif response.status_code >= 500:
                    message = "AllModels is temporarily unavailable. Try again shortly."
                else:
                    message = detail or "AllModels rejected the request."
            if used_key:
                message = message.replace(used_key, "[REDACTED]")
            raise AllModelsAPIError(message[:800], code=code, status_code=response.status_code)
        if not isinstance(payload, dict):
            raise AllModelsAPIError("AllModels returned an unexpected response.")
        return payload

    def list_models(self, api_key: str = "") -> Dict[str, Any]:
        return self._request_json("GET", "/v1/models", api_key=api_key)

    def list_voices(self, api_key: str = "") -> Dict[str, Any]:
        return self._request_json("GET", "/v1/voices", api_key=api_key)

    def start_signup(self, email: str) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/account/agent-signup",
            authenticated=False,
            json_body={"email": email},
        )

    def verify_signup(self, email: str, code: str) -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/account/agent-signup/verify",
            authenticated=False,
            json_body={"email": email, "code": code},
        )

    def get_balance(self, api_key: str = "") -> Dict[str, Any]:
        return self._request_json("GET", "/account/balance", api_key=api_key)

    def create_topup(self, amount_usd: Decimal, api_key: str = "") -> Dict[str, Any]:
        return self._request_json(
            "POST",
            "/account/top-up-link",
            api_key=api_key,
            json_body={"amount_usd": float(amount_usd)},
            headers={"idempotency-key": str(uuid.uuid4())},
        )
