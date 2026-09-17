from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx


def test_catalog_persists_and_recovers_from_corrupt_cache(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg.catalog import CatalogStore

    class Client:
        def list_models(self, api_key=""):
            return sample_models

        def list_voices(self, api_key="", **_kwargs):
            return {"voices": sample_voices}

    cache_path = hermes_home / "cache" / "hermes-speech" / "catalog.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("not-json", encoding="utf-8")
    store = CatalogStore(Client())
    catalog = store.ensure("key", cold_timeout=2)
    assert catalog["models"]["tts"][0]["id"].startswith("elevenlabs/")
    assert json.loads(cache_path.read_text())["version"] == 2


def test_catalog_keeps_stale_data_on_refresh_error(
    speech_pkg, hermes_home, sample_catalog
):
    from hermes_speech_testpkg.catalog import CatalogStore
    from hermes_speech_testpkg.client import AllModelsAPIError

    class Client:
        def list_models(self, api_key=""):
            raise AllModelsAPIError("offline")

        def list_voices(self, api_key="", **_kwargs):
            raise AssertionError

    store = CatalogStore(Client())
    store.replace_for_tests(sample_catalog)
    assert store.ensure("key", cold_timeout=0) is sample_catalog


def test_catalog_cold_fetch_is_bounded_and_concurrent_refreshes_coalesce(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg.catalog import CatalogStore

    started = threading.Event()
    release = threading.Event()

    class Client:
        calls = 0

        def list_models(self, api_key=""):
            self.calls += 1
            started.set()
            release.wait(2)
            return sample_models

        def list_voices(self, api_key="", **_kwargs):
            return {"voices": sample_voices}

    client = Client()
    store = CatalogStore(client)
    before = time.monotonic()
    assert store.ensure("key", cold_timeout=0.01) is None
    assert time.monotonic() - before < 0.25
    assert started.is_set()
    assert store.ensure("key", cold_timeout=0) is None
    assert client.calls == 1
    release.set()
    for _ in range(100):
        if store.cached() is not None:
            break
        time.sleep(0.01)
    assert store.cached() is not None


def test_client_maps_auth_errors_without_leaking_key(speech_pkg, monkeypatch):
    from hermes_speech_testpkg.client import AllModelsAPIError, AllModelsClient

    def request(*args, **kwargs):
        return httpx.Response(
            401,
            json={"error": "invalid_api_key"},
            request=httpx.Request("GET", "https://api.allmodels.io/v1/models"),
        )

    monkeypatch.setattr(httpx, "request", request)
    client = AllModelsClient()
    try:
        client.list_models("super-secret")
    except AllModelsAPIError as exc:
        assert exc.is_auth_error
        assert "super-secret" not in str(exc)
    else:
        raise AssertionError("expected AllModelsAPIError")


def test_client_voice_search_uses_public_query_parameters(speech_pkg, monkeypatch):
    from hermes_speech_testpkg.client import AllModelsClient

    captured = {}

    def request(method, url, **kwargs):
        captured.update(method=method, url=url, kwargs=kwargs)
        return httpx.Response(
            200,
            json={
                "voices": [],
                "facets": [],
                "total_count": 0,
                "has_more": False,
                "next_cursor": None,
                "catalogue_updated_at": "2026-08-18T00:00:00Z",
            },
        )

    monkeypatch.setattr(httpx, "request", request)
    AllModelsClient().list_voices(
        query="british female",
        model="elevenlabs/eleven-v3",
        page_size=10,
        include_facets=True,
    )

    assert captured["method"] == "GET"
    assert captured["url"].endswith("/v1/voices")
    assert captured["kwargs"]["params"] == {
        "page_size": 10,
        "q": "british female",
        "model": "elevenlabs/eleven-v3",
        "include_facets": "true",
    }
    assert "Authorization" not in captured["kwargs"]["headers"]


def test_voice_search_normalizes_rich_rows_but_caches_only_compact_metadata(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg.catalog import CatalogStore
    from hermes_speech_testpkg.client import AllModelsAPIError

    sample_voices[0]["voice"]["labels"].append(
        {
            "facet": {"id": "accent", "name": "Accent"},
            "value": {"id": "british", "name": "British"},
        }
    )

    class Client:
        offline = False

        def list_models(self, api_key=""):
            return sample_models

        def list_voices(self, **_kwargs):
            if self.offline:
                raise AllModelsAPIError("offline")
            return {
                "voices": [sample_voices[0]],
                "facets": [{"id": "label.gender", "name": "Gender", "values": []}],
                "total_count": 1,
                "has_more": False,
                "next_cursor": None,
                "catalogue_updated_at": "2026-08-18T00:00:00Z",
            }

    client = Client()
    store = CatalogStore(client)
    assert store.ensure("key", cold_timeout=2)
    page = store.search_voices(query="Aria", page_size=10, include_facets=True)

    assert page["voices"][0]["gender"] == "female"
    assert page["voices"][0]["preview_url"].startswith("https://audio.example/")
    assert page["voices"][0]["labels"]["gender"][0]["id"] == "female"
    cached = store.cached()["voice_entries"][0]
    assert cached["name"] == "Aria"
    assert "preview_url" not in cached
    assert "labels" not in cached
    assert "metrics" not in cached
    assert "British" in cached["keywords"]

    client.offline = True
    fallback = store.search_voices(query="british female", page_size=10)
    assert fallback["stale"] is True
    assert fallback["voices"][0]["name"] == "Aria"


def test_plugin_registers_command_and_both_providers(speech_pkg, hermes_home):
    from hermes_cli.config import read_raw_config, save_config

    existing = hermes_home / "team-skills"
    existing.mkdir()
    save_config(
        {
            "model": {"provider": "xai", "default": "grok-4"},
            "skills": {"external_dirs": [str(existing)]},
        },
        strip_defaults=False,
    )
    config_before = read_raw_config()
    registered = {
        "tts": [],
        "stt": [],
        "commands": [],
        "tools": [],
        "skills": [],
    }

    class Context:
        def register_tts_provider(self, provider):
            registered["tts"].append(provider)

        def register_transcription_provider(self, provider):
            registered["stt"].append(provider)

        def register_command(self, name, **kwargs):
            registered["commands"].append((name, kwargs))

        def register_tool(self, **kwargs):
            registered["tools"].append(kwargs)

        def register_skill(self, name, path, **kwargs):
            registered["skills"].append((name, Path(path), kwargs))

    speech_pkg.register(Context())
    assert [provider.name for provider in registered["tts"]] == ["allmodels"]
    assert [provider.name for provider in registered["stt"]] == ["allmodels"]
    assert registered["commands"][0][0] == "speech"
    assert registered["commands"][0][1]["args_hint"]
    assert registered["tools"][0]["name"] == "allmodels_speech_setup"
    assert registered["tools"][0]["toolset"] == "allmodels_speech"
    assert "requires_env" not in registered["tools"][0]
    assert registered["tools"][1]["name"] == "allmodels_speech_manage"
    assert registered["tools"][1]["toolset"] == "allmodels_speech"
    assert "requires_env" not in registered["tools"][1]
    assert [name for name, _path, _kwargs in registered["skills"]] == [
        "configure-allmodels-speech",
        "manage-allmodels-speech",
    ]
    assert all(path.is_file() for _name, path, _kwargs in registered["skills"])
    from tools.tts_streaming import _REGISTRY

    assert "allmodels" in _REGISTRY
    assert read_raw_config() == config_before


def test_manifest_declares_registered_capabilities_and_compatibility(speech_pkg):
    import yaml

    root = Path(speech_pkg.__file__).resolve().parent
    manifest = yaml.safe_load((root / "plugin.yaml").read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 1
    assert manifest["version"] == "0.3.1"
    assert manifest["requires_hermes"] == ">=0.20.0"
    assert manifest["requires_env"] == []
    assert manifest["provides_tools"] == [
        "allmodels_speech_setup",
        "allmodels_speech_manage",
    ]
    assert manifest["provides_hooks"] == []
    assert manifest["provides_middleware"] == []


def test_setup_skill_has_no_template_placeholders(speech_pkg):
    root = Path(speech_pkg.__file__).resolve().parent
    content = (root / "skills" / "configure-allmodels-speech" / "SKILL.md").read_text()
    assert "TODO" not in content
    assert "allmodels_speech_setup" in content
    assert "name: configure-allmodels-speech" in content
    assert "requires_toolsets:" not in content
    assert "tool_describe" in content
    assert "tool_call" in content

    management = (root / "skills" / "manage-allmodels-speech" / "SKILL.md").read_text()
    assert "TODO" not in management
    assert "allmodels_speech_manage" in management
    assert "name: manage-allmodels-speech" in management
    assert "requires_toolsets:" not in management
    assert "requires_tools:" not in management
    assert "tool_describe" in management
    assert "tool_call" in management
    assert "hermes plugins update hermes-speech" in management
    assert "update_plugin" not in management
