from __future__ import annotations

import json
import threading
from pathlib import Path

import httpx


class FakeResponse:
    def __init__(
        self,
        status_code=200,
        *,
        version="0.2.0",
        etag='"release-2"',
        draft=False,
        prerelease=False,
    ):
        self.status_code = status_code
        self.headers = {"etag": etag}
        self._version = version
        self._draft = draft
        self._prerelease = prerelease

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "failed",
                request=httpx.Request("GET", "https://api.github.com/test"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return {
            "tag_name": f"v{self._version}",
            "html_url": f"https://github.com/allmodels-io/hermes-speech/releases/tag/v{self._version}",
            "draft": self._draft,
            "prerelease": self._prerelease,
        }


def plugin_dir(tmp_path: Path, version: str = "0.1.0") -> Path:
    path = tmp_path / "source"
    path.mkdir()
    (path / "plugin.yaml").write_text(
        f"name: hermes-speech\nversion: {version}\n",
        encoding="utf-8",
    )
    return path


def test_explicit_check_uses_stable_release_and_sends_no_user_data(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    captured = {}

    def request(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path), request=request, **update_checker_paths
    )
    result = checker.check_now()

    assert result["update_available"] is True
    assert result["current_version"] == "0.1.0"
    assert result["latest_version"] == "0.2.0"
    assert captured["url"].endswith("/allmodels-io/hermes-speech/releases/latest")
    serialized = json.dumps(captured)
    assert "ALLMODELS_API_KEY" not in serialized
    assert "fish/s2-1-pro" not in serialized
    assert "voice" not in serialized.lower()
    assert captured["kwargs"]["timeout"] == 2.0


def test_background_checks_coalesce_and_cached_notice_is_rate_limited(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    started = threading.Event()
    release = threading.Event()
    calls = 0
    now = [1000.0]

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(2)
        return FakeResponse()

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=request,
        clock=lambda: now[0],
        check_interval=100,
        reminder_interval=700,
        **update_checker_paths,
    )
    checker.start_background_check()
    checker.start_background_check()
    assert started.wait(1)
    assert calls == 1
    assert checker.maybe_notification() is None
    release.set()
    thread = checker._refresh_thread
    assert thread is not None
    thread.join(2)

    notice = checker.maybe_notification()
    assert notice["latest_version"] == "0.2.0"
    assert notice["update_available"] is True
    assert notice["update_performed"] is False
    assert notice["next_action"] == "ask_agent_to_update_plugin"
    assert notice["suggested_request"] == "Update the hermes-speech plugin."
    assert notice["restart_required_after_update"] is True
    assert "update_command" not in notice
    assert checker.maybe_notification() is None
    assert calls == 1

    now[0] += 701
    reminder = checker.maybe_notification()
    assert reminder["latest_version"] == "0.2.0"
    thread = checker._refresh_thread
    if thread is not None:
        thread.join(2)
    assert checker._refresh_thread is None


def test_failed_background_attempt_is_cached_for_the_check_interval(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    calls = 0
    now = [1000.0]

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline")

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=request,
        clock=lambda: now[0],
        check_interval=100,
        **update_checker_paths,
    )
    checker.start_background_check()
    thread = checker._refresh_thread
    assert thread is not None
    thread.join(2)
    checker.start_background_check()
    assert calls == 1

    now[0] += 101
    checker.start_background_check()
    thread = checker._refresh_thread
    assert thread is not None
    thread.join(2)
    assert calls == 2


def test_offline_explicit_check_returns_stale_cached_release(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=lambda *_args, **_kwargs: FakeResponse(),
        **update_checker_paths,
    )
    assert checker.check_now()["latest_version"] == "0.2.0"

    def offline(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    checker._request = offline
    stale = checker.check_now()

    assert stale["success"] is True
    assert stale["stale"] is True
    assert stale["error"] == "update_check_failed"
    assert stale["latest_version"] == "0.2.0"
    assert stale["update_available"] is True


def test_automatic_check_can_be_disabled_without_blocking_explicit_check(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_cli.config import save_config
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    calls = 0

    def request(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    save_config(
        {"plugins": {"hermes-speech": {"update_check": False}}},
        strip_defaults=False,
    )
    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path), request=request, **update_checker_paths
    )
    checker.start_background_check()
    assert checker.maybe_notification() is None
    assert calls == 0
    assert checker.check_now()["latest_version"] == "0.2.0"
    assert calls == 1


def test_explicit_check_is_current_when_release_matches_installed_version(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path, version="0.2.0"),
        request=lambda *_args, **_kwargs: FakeResponse(),
        **update_checker_paths,
    )
    result = checker.check_now()

    assert result["success"] is True
    assert result["update_available"] is False
    assert checker.format_check() == "Hermes Speech is up to date (version 0.2.0)."


def test_explicit_check_never_modifies_plugin_source(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    source = plugin_dir(tmp_path)
    sentinel = source / "sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    before = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }
    checker = PluginUpdateChecker(
        plugin_dir=source,
        request=lambda *_args, **_kwargs: FakeResponse(),
        **update_checker_paths,
    )
    message = checker.format_check()
    after = {
        path.relative_to(source): path.read_bytes()
        for path in source.rglob("*")
        if path.is_file()
    }

    assert "Ask your Hermes agent" in message
    assert before == after


def test_json_decoration_never_overwrites_tool_result(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=lambda *_args, **_kwargs: FakeResponse(),
        **update_checker_paths,
    )
    checker.check_now()
    payload = json.loads(checker.decorate_json('{"success":true,"balance":"2.00"}'))
    assert payload["success"] is True
    assert payload["balance"] == "2.00"
    assert payload["plugin_update"]["latest_version"] == "0.2.0"
    assert payload["plugin_update"]["update_performed"] is False


def test_prerelease_is_not_accepted_as_latest_stable_release(
    speech_pkg, hermes_home, update_checker_paths, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=lambda *_args, **_kwargs: FakeResponse(
            version="0.3.0-rc.1", prerelease=True
        ),
        **update_checker_paths,
    )
    result = checker.check_now()

    assert result == {"success": False, "error": "invalid_release_response"}
