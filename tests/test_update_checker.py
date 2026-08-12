from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import httpx


class FakeResponse:
    def __init__(self, status_code=200, *, version="0.2.0", etag='"release-2"'):
        self.status_code = status_code
        self.headers = {"etag": etag}
        self._version = version

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
            "draft": False,
            "prerelease": False,
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
    speech_pkg, hermes_home, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    captured = {}

    def request(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return FakeResponse()

    checker = PluginUpdateChecker(plugin_dir=plugin_dir(tmp_path), request=request)
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
    speech_pkg, hermes_home, tmp_path
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
    assert notice["update_command"] == "/speech update"
    assert checker.maybe_notification() is None
    assert calls == 1

    now[0] += 701
    reminder = checker.maybe_notification()
    assert reminder["latest_version"] == "0.2.0"


def test_failed_background_attempt_is_cached_for_the_check_interval(
    speech_pkg, hermes_home, tmp_path
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


def test_automatic_check_can_be_disabled_without_blocking_explicit_check(
    speech_pkg, hermes_home, tmp_path
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
    checker = PluginUpdateChecker(plugin_dir=plugin_dir(tmp_path), request=request)
    checker.start_background_check()
    assert checker.maybe_notification() is None
    assert calls == 0
    assert checker.check_now()["latest_version"] == "0.2.0"
    assert calls == 1


def test_explicit_update_reports_restart_and_uses_injected_updater(
    speech_pkg, hermes_home, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    updates = 0

    def updater():
        nonlocal updates
        updates += 1
        return {"success": True, "installed_version": "0.2.0"}

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=lambda *_args, **_kwargs: FakeResponse(),
        updater=updater,
    )
    result = checker.update_now()
    assert result == {
        "success": True,
        "updated": True,
        "previous_version": "0.1.0",
        "installed_version": "0.2.0",
        "restart_required": True,
        "instruction": "Restart Hermes to load the updated plugin.",
    }
    assert updates == 1


def test_linked_development_install_is_never_modified(
    speech_pkg, hermes_home, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    source = plugin_dir(tmp_path)
    plugins = hermes_home / "plugins"
    plugins.mkdir()
    (plugins / "hermes-speech").symlink_to(source, target_is_directory=True)
    checker = PluginUpdateChecker(
        plugin_dir=source,
        request=lambda *_args, **_kwargs: FakeResponse(),
    )
    result = checker.update_now()
    assert result["success"] is False
    assert result["error"] == "development_symlink"


def test_json_decoration_never_overwrites_tool_result(
    speech_pkg, hermes_home, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    checker = PluginUpdateChecker(
        plugin_dir=plugin_dir(tmp_path),
        request=lambda *_args, **_kwargs: FakeResponse(),
    )
    checker.check_now()
    payload = json.loads(checker.decorate_json('{"success":true,"balance":"2.00"}'))
    assert payload["success"] is True
    assert payload["balance"] == "2.00"
    assert payload["plugin_update"]["latest_version"] == "0.2.0"


def test_explicit_update_uses_hermes_real_git_updater(
    speech_pkg, hermes_home, tmp_path
):
    from hermes_speech_testpkg.update_checker import PluginUpdateChecker

    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    target = hermes_home / "plugins" / "hermes-speech"

    def git(*args, cwd=None):
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "--bare", str(remote))
    git("init", "-b", "main", str(seed))
    (seed / "plugin.yaml").write_text(
        "name: hermes-speech\nversion: 0.1.0\n",
        encoding="utf-8",
    )
    git("add", "plugin.yaml", cwd=seed)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "v0.1.0", cwd=seed)
    git("remote", "add", "origin", str(remote), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    target.parent.mkdir(parents=True)
    git("clone", str(remote), str(target))

    (seed / "plugin.yaml").write_text(
        "name: hermes-speech\nversion: 0.2.0\n",
        encoding="utf-8",
    )
    git("add", "plugin.yaml", cwd=seed)
    git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "v0.2.0", cwd=seed)
    git("push", cwd=seed)

    checker = PluginUpdateChecker(
        plugin_dir=target,
        request=lambda *_args, **_kwargs: FakeResponse(),
    )
    checker._official_remote = lambda _remote: True
    result = checker.update_now()

    assert result["success"] is True
    assert result["updated"] is True
    assert result["installed_version"] == "0.2.0"
    assert _plugin_version(target) == "0.2.0"
    assert not (target / "__pycache__").exists()


def _plugin_version(path: Path) -> str:
    for line in (path / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    raise AssertionError("plugin version missing")
