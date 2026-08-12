"""Non-blocking release checks and explicit self-updates for hermes-speech."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import httpx
import yaml

PLUGIN_NAME = "hermes-speech"
REPOSITORY = "allmodels-io/hermes-speech"
RELEASES_API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
CHECK_INTERVAL_SECONDS = 24 * 60 * 60
REMINDER_INTERVAL_SECONDS = 7 * 24 * 60 * 60
_CACHE_VERSION = 1
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def _version_parts(value: str) -> Optional[tuple[int, int, int, Optional[str]]]:
    match = _SEMVER_RE.fullmatch(str(value or "").strip())
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3]), match[4]


def _is_newer(candidate: str, current: str) -> bool:
    candidate_parts = _version_parts(candidate)
    current_parts = _version_parts(current)
    if candidate_parts is None or current_parts is None:
        return False
    candidate_core = candidate_parts[:3]
    current_core = current_parts[:3]
    if candidate_core != current_core:
        return candidate_core > current_core
    # A stable release is newer than a prerelease of the same core version.
    return candidate_parts[3] is None and current_parts[3] is not None


def _manifest_version(path: Path) -> str:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError, TypeError):
        return "0.0.0"
    return str(payload.get("version") or "0.0.0").strip()


class PluginUpdateChecker:
    """Profile-scoped release cache with notification and explicit update APIs."""

    def __init__(
        self,
        *,
        plugin_dir: Optional[Path] = None,
        request: Optional[Callable[..., Any]] = None,
        clock: Callable[[], float] = time.time,
        check_interval: float = CHECK_INTERVAL_SECONDS,
        reminder_interval: float = REMINDER_INTERVAL_SECONDS,
        updater: Optional[Callable[[], Dict[str, Any]]] = None,
    ) -> None:
        self.plugin_dir = (plugin_dir or Path(__file__).resolve().parent).resolve()
        self._request = request or httpx.get
        self._clock = clock
        self.check_interval = check_interval
        self.reminder_interval = reminder_interval
        self._updater = updater or self._update_git_install
        self._lock = threading.RLock()
        self._loaded_path: Optional[Path] = None
        self._cache: Dict[str, Any] = {}
        self._refresh_thread: Optional[threading.Thread] = None

    @property
    def current_version(self) -> str:
        return _manifest_version(self.plugin_dir / "plugin.yaml")

    @staticmethod
    def _cache_path() -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "cache" / "hermes-speech" / "update.json"

    @staticmethod
    def _installed_path() -> Path:
        from hermes_constants import get_hermes_home

        return get_hermes_home() / "plugins" / PLUGIN_NAME

    @staticmethod
    def automatic_enabled() -> bool:
        try:
            from hermes_cli.config import load_config

            config = load_config()
            plugins = config.get("plugins") if isinstance(config, dict) else None
            section = plugins.get(PLUGIN_NAME) if isinstance(plugins, dict) else None
            if isinstance(section, dict) and section.get("update_check") is False:
                return False
        except Exception:
            pass
        return True

    def _load_locked(self) -> Dict[str, Any]:
        path = self._cache_path()
        if self._loaded_path == path:
            return self._cache
        self._loaded_path = path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        self._cache = payload if isinstance(payload, dict) and payload.get("version") == _CACHE_VERSION else {}
        return self._cache

    def _write_locked(self) -> None:
        path = self._cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(self._cache)
        payload["version"] = _CACHE_VERSION
        fd, tmp_name = tempfile.mkstemp(prefix="update-", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _cached_status_locked(self) -> Optional[Dict[str, Any]]:
        cache = self._load_locked()
        latest = str(cache.get("latest_version") or "").strip()
        if _version_parts(latest) is None:
            return None
        current = self.current_version
        return {
            "success": True,
            "current_version": current,
            "latest_version": latest,
            "update_available": _is_newer(latest, current),
            "release_url": str(cache.get("release_url") or RELEASES_URL),
            "checked_at": cache.get("checked_at"),
            "cached": True,
        }

    def _fetch_latest(self) -> Dict[str, Any]:
        with self._lock:
            cache = self._load_locked()
            etag = str(cache.get("etag") or "").strip()
            cache["attempted_at"] = self._clock()
            try:
                self._write_locked()
            except OSError:
                pass
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"hermes-speech/{self.current_version}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if etag:
            headers["If-None-Match"] = etag
        try:
            response = self._request(
                RELEASES_API_URL,
                headers=headers,
                timeout=2.0,
                follow_redirects=False,
            )
            if response.status_code == 304:
                with self._lock:
                    cached = self._cached_status_locked()
                    if cached is None:
                        return {"success": False, "error": "update_check_failed"}
                    self._cache["checked_at"] = self._clock()
                    try:
                        self._write_locked()
                    except OSError:
                        pass
                    cached["checked_at"] = self._cache["checked_at"]
                    return cached
            if response.status_code == 404:
                return {
                    "success": False,
                    "error": "no_published_release",
                    "release_url": RELEASES_URL,
                }
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            with self._lock:
                stale = self._cached_status_locked()
            if stale is not None:
                stale.update(stale=True, error="update_check_failed")
                return stale
            return {"success": False, "error": "update_check_failed"}

        if not isinstance(payload, dict) or payload.get("draft") is True or payload.get("prerelease") is True:
            return {"success": False, "error": "invalid_release_response"}
        latest = str(payload.get("tag_name") or "").strip().removeprefix("v")
        if _version_parts(latest) is None or _version_parts(latest)[3] is not None:
            return {"success": False, "error": "invalid_release_version"}
        release_url = str(payload.get("html_url") or RELEASES_URL)
        with self._lock:
            cache = self._load_locked()
            cache.update(
                {
                    "version": _CACHE_VERSION,
                    "latest_version": latest,
                    "release_url": release_url,
                    "checked_at": self._clock(),
                    "etag": str(response.headers.get("etag") or ""),
                }
            )
            try:
                self._write_locked()
            except OSError:
                pass
        return {
            "success": True,
            "current_version": self.current_version,
            "latest_version": latest,
            "update_available": _is_newer(latest, self.current_version),
            "release_url": release_url,
            "checked_at": cache["checked_at"],
            "cached": False,
        }

    def _refresh_worker(self) -> None:
        try:
            self._fetch_latest()
        finally:
            with self._lock:
                self._refresh_thread = None

    def start_background_check(self) -> None:
        if not self.automatic_enabled():
            return
        with self._lock:
            cache = self._load_locked()
            attempted_at = float(cache.get("attempted_at") or cache.get("checked_at") or 0)
            if self._clock() - attempted_at < self.check_interval:
                return
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._refresh_worker,
                name="hermes-speech-update-check",
                daemon=True,
            )
            self._refresh_thread = thread
            thread.start()

    def check_now(self) -> Dict[str, Any]:
        """Perform an explicit fresh stable-release check."""
        with self._lock:
            running = self._refresh_thread
        if running is not None and running.is_alive():
            running.join(2.5)
            with self._lock:
                cached = self._cached_status_locked()
            if cached is not None and not running.is_alive():
                return cached
            if running.is_alive():
                if cached is not None:
                    cached.update(stale=True, error="update_check_in_progress")
                    return cached
                return {"success": False, "error": "update_check_in_progress"}
        return self._fetch_latest()

    def maybe_notification(self) -> Optional[Dict[str, Any]]:
        """Start a due refresh and return a rate-limited cached update notice."""
        if not self.automatic_enabled():
            return None
        self.start_background_check()
        with self._lock:
            status = self._cached_status_locked()
            if status is None or not status["update_available"]:
                return None
            cache = self._load_locked()
            notified_version = str(cache.get("notified_version") or "")
            notified_at = float(cache.get("notified_at") or 0)
            if (
                notified_version == status["latest_version"]
                and self._clock() - notified_at < self.reminder_interval
            ):
                return None
            cache["notified_version"] = status["latest_version"]
            cache["notified_at"] = self._clock()
            try:
                self._write_locked()
            except OSError:
                pass
        return self._public_update(status)

    @staticmethod
    def _public_update(status: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "current_version": status.get("current_version"),
            "latest_version": status.get("latest_version"),
            "release_url": status.get("release_url") or RELEASES_URL,
            "update_command": "/speech update",
            "restart_required": True,
        }

    def decorate_json(self, raw_result: str) -> str:
        notice = self.maybe_notification()
        if notice is None:
            return raw_result
        try:
            payload = json.loads(raw_result)
        except (TypeError, ValueError):
            return raw_result
        if not isinstance(payload, dict):
            return raw_result
        payload["plugin_update"] = notice
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def decorate_text(self, result: str) -> str:
        notice = self.maybe_notification()
        if notice is None:
            return result
        return (
            f"{result}\n\n"
            f"Hermes Speech update available: {notice['current_version']} → "
            f"{notice['latest_version']}. Run `/speech update`, then restart Hermes.\n"
            f"Release: {notice['release_url']}"
        )

    def format_check(self) -> str:
        status = self.check_now()
        if not status.get("success"):
            if status.get("error") == "no_published_release":
                return f"No stable Hermes Speech release is published yet.\n{RELEASES_URL}"
            return "Hermes Speech could not check for updates. Try again later."
        if status.get("update_available"):
            return (
                f"Hermes Speech {status['latest_version']} is available "
                f"(installed: {status['current_version']}).\n"
                "Run `/speech update` to install it.\n"
                f"Release: {status['release_url']}"
            )
        return f"Hermes Speech is up to date (version {status['current_version']})."

    def update_now(self) -> Dict[str, Any]:
        status = self.check_now()
        if not status.get("success"):
            return status
        if not status.get("update_available"):
            return {
                "success": True,
                "updated": False,
                "current_version": status["current_version"],
                "latest_version": status["latest_version"],
                "restart_required": False,
            }
        result = self._updater()
        if not result.get("success"):
            result.setdefault("current_version", status["current_version"])
            result.setdefault("latest_version", status["latest_version"])
            result.setdefault("release_url", status.get("release_url") or RELEASES_URL)
            return result
        installed_version = str(result.get("installed_version") or self.current_version)
        if _is_newer(status["latest_version"], installed_version):
            return {
                "success": False,
                "error": "updated_checkout_does_not_contain_latest_release",
                "current_version": installed_version,
                "latest_version": status["latest_version"],
                "release_url": status.get("release_url") or RELEASES_URL,
            }
        with self._lock:
            cache = self._load_locked()
            cache["notified_version"] = status["latest_version"]
            cache["notified_at"] = self._clock()
            try:
                self._write_locked()
            except OSError:
                pass
        return {
            "success": True,
            "updated": True,
            "previous_version": status["current_version"],
            "installed_version": installed_version,
            "restart_required": True,
            "instruction": "Restart Hermes to load the updated plugin.",
        }

    def format_update(self) -> str:
        result = self.update_now()
        if result.get("success") and result.get("updated"):
            return (
                f"Updated Hermes Speech from {result['previous_version']} to "
                f"{result['installed_version']}.\n\nRestart Hermes to load the new version."
            )
        if result.get("success"):
            return f"Hermes Speech is already up to date (version {result['current_version']})."
        errors = {
            "development_symlink": (
                "This is a linked development installation, so `/speech update` will not "
                "modify its source. Update the linked checkout and restart Hermes."
            ),
            "not_git_install": (
                "This Hermes Speech installation is not a Git checkout and cannot update "
                f"itself. Reinstall it from {RELEASES_URL}."
            ),
            "local_changes": (
                "Hermes Speech has local changes, so the update was refused to avoid "
                "overwriting them. Commit or remove those changes, then try again."
            ),
            "unexpected_remote": (
                "Hermes Speech was installed from a different Git remote. Update that "
                "checkout manually rather than replacing its source."
            ),
            "no_published_release": f"No stable Hermes Speech release is published yet.\n{RELEASES_URL}",
            "update_check_failed": "Hermes Speech could not check for updates. Try again later.",
            "plugin_update_failed": "Hermes could not update the plugin. Run `hermes plugins update hermes-speech` for details.",
            "mismatched_install": (
                "The active Hermes Speech code does not match the profile's installed plugin. "
                "Restart Hermes from the intended profile before updating."
            ),
            "updated_checkout_does_not_contain_latest_release": (
                "The Git checkout updated, but it does not contain the latest published "
                f"version. See {result.get('release_url') or RELEASES_URL}."
            ),
        }
        return errors.get(str(result.get("error") or ""), "Hermes Speech could not update itself.")

    @staticmethod
    def _official_remote(value: str) -> bool:
        normalized = value.strip().lower().removesuffix(".git").removesuffix("/")
        return normalized in {
            "git@github.com:allmodels-io/hermes-speech",
            "https://github.com/allmodels-io/hermes-speech",
            "ssh://git@github.com/allmodels-io/hermes-speech",
        }

    def _git_output(self, target: Path, *args: str) -> Optional[str]:
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=str(target),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout.strip() if completed.returncode == 0 else None

    def _update_git_install(self) -> Dict[str, Any]:
        target = self._installed_path()
        if target.is_symlink():
            return {"success": False, "error": "development_symlink", "source_path": str(target.resolve())}
        if not target.is_dir() or not (target / ".git").exists():
            return {"success": False, "error": "not_git_install"}
        if target.resolve() != self.plugin_dir:
            return {"success": False, "error": "mismatched_install"}
        dirty = self._git_output(target, "status", "--porcelain")
        if dirty is None:
            return {"success": False, "error": "plugin_update_failed"}
        if dirty:
            return {"success": False, "error": "local_changes"}
        remote = self._git_output(target, "remote", "get-url", "origin")
        if remote is None or not self._official_remote(remote):
            return {"success": False, "error": "unexpected_remote"}

        from hermes_cli.plugins_cmd import dashboard_update_user_plugin

        result = dashboard_update_user_plugin(PLUGIN_NAME)
        if not result.get("ok"):
            return {"success": False, "error": "plugin_update_failed"}
        return {
            "success": True,
            "installed_version": _manifest_version(target / "plugin.yaml"),
            "unchanged": bool(result.get("unchanged")),
        }
