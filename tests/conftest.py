from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "hermes_speech_testpkg"


def _load_package():
    existing = sys.modules.get(PACKAGE)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def speech_pkg():
    return _load_package()


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("ALLMODELS_API_KEY", raising=False)
    try:
        from hermes_cli.config import invalidate_env_cache

        invalidate_env_cache()
    except Exception:
        pass
    yield home


@pytest.fixture
def update_checker_paths(hermes_home):
    """Keep update-checker state inside the test home, even across threads."""
    return {
        "cache_path": hermes_home / "cache" / "hermes-speech" / "update.json",
        "installed_path": hermes_home / "plugins" / "hermes-speech",
    }


@pytest.fixture
def sample_models():
    return {
        "tts": [
            {
                "id": "elevenlabs/eleven-turbo-v2-5",
                "aliases": ["elevenlabs/eleven_turbo_v2_5"],
                "providers": [
                    {
                        "id": "elevenlabs",
                        "modelId": "eleven_turbo_v2_5",
                        "synchronous": True,
                        "streaming": True,
                    },
                    {
                        "id": "fal",
                        "modelId": "fal-ai/elevenlabs/tts/turbo-v2.5",
                        "synchronous": True,
                        "streaming": True,
                    },
                ],
            },
            {
                "id": "elevenlabs/eleven-multilingual-v2",
                "aliases": [],
                "providers": [
                    {
                        "id": "elevenlabs",
                        "modelId": "eleven_multilingual_v2",
                        "synchronous": True,
                        "streaming": True,
                    }
                ],
            },
            {
                "id": "openai/gpt-4o-mini-tts",
                "aliases": [],
                "providers": [
                    {
                        "id": "openai",
                        "modelId": "gpt-4o-mini-tts",
                        "synchronous": True,
                        "streaming": False,
                    }
                ],
            },
            {
                "id": "fish/s2-1-pro",
                "aliases": ["fish/s2.1-pro"],
                "providers": [
                    {
                        "id": "fish",
                        "modelId": "s2.1-pro",
                        "synchronous": True,
                        "streaming": True,
                    }
                ],
            },
        ],
        "stt": [
            {
                "id": "openai/whisper-1",
                "aliases": [],
                "providers": [
                    {
                        "id": "openai",
                        "modelId": "whisper-1",
                        "synchronous": True,
                        "streaming": False,
                        "batch": False,
                    }
                ],
            },
            {
                "id": "deepgram/nova-3",
                "aliases": [],
                "providers": [
                    {
                        "id": "deepgram",
                        "modelId": "nova-3",
                        "synchronous": True,
                        "streaming": True,
                        "batch": False,
                    }
                ],
            },
            {
                "id": "soniox/stt-async-v5",
                "aliases": [],
                "providers": [
                    {
                        "id": "soniox",
                        "modelId": "stt-async-v5",
                        "synchronous": True,
                        "streaming": False,
                        "batch": True,
                    }
                ],
            },
        ],
    }


@pytest.fixture
def sample_voices():
    def row(
        model,
        voice_id,
        name,
        provider,
        *,
        description=None,
        gender=None,
        language=None,
        default=False,
    ):
        labels = []
        if gender:
            labels.append(
                {
                    "facet": {"id": "gender", "name": "Gender"},
                    "value": {"id": gender, "name": gender.title()},
                }
            )
        return {
            "model": {"id": model, "name": model},
            "voice": {
                "id": voice_id,
                "name": name,
                "description": description,
                "category": {"id": "official", "name": "Official"},
                "preview_url": f"https://audio.example/{voice_id}.mp3",
                "languages": ([{"id": language, "name": language}] if language else []),
                "labels": labels,
                "source_urls": [],
                "scope": {"type": "global"},
                "first_seen_at": "2026-08-17T00:00:00Z",
                "last_seen_at": "2026-08-18T00:00:00Z",
            },
            "providers": [
                {
                    "id": provider,
                    "name": provider.title(),
                    "default": default,
                    "provider_model_id": model.partition("/")[2],
                    "metrics": None,
                }
            ],
        }

    return [
        row(
            "elevenlabs/eleven-turbo-v2-5",
            "voice-a",
            "Aria",
            "elevenlabs",
            gender="female",
            language="en",
            default=True,
        ),
        row("elevenlabs/eleven-multilingual-v2", "voice-b", "Brian", "elevenlabs"),
        row("elevenlabs/eleven-turbo-v2-5", "fal-voice", "Fal Voice", "fal"),
        row("openai/gpt-4o-mini-tts", "alloy", "alloy", "openai", default=True),
        row("openai/gpt-4o-mini-tts", "ballad", "ballad", "openai"),
        row(
            "fish/s2-1-pro",
            "03397b4c4be74759b72533b663fbd001",
            "Elon Musk(Noise reduction)",
            "fish",
            language="en",
        ),
        row("fish/s2-1-pro", "another-fish-voice", "Another Fish Voice", "fish"),
    ]


@pytest.fixture
def sample_catalog(speech_pkg, sample_models, sample_voices):
    from hermes_speech_testpkg.catalog import _normalize_voice_entry

    return {
        "version": 2,
        "fetched_at": 1.0,
        "models": sample_models,
        "voice_entries": [
            voice for row in sample_voices for voice in _normalize_voice_entry(row)
        ],
    }
