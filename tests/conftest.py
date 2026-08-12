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
    return {
        "elevenlabs": {
            "default": "voice-a",
            "source": "live",
            "voices": [
                {
                    "id": "voice-a",
                    "name": "Aria",
                    "gender": "female",
                    "models": ["eleven_turbo_v2_5"],
                },
                {"id": "voice-b", "name": "Brian", "models": ["eleven_multilingual_v2"]},
            ],
        },
        "fal": {
            "source": "static",
            "voices": [{"id": "fal-voice", "name": "Fal Voice"}],
        },
        "openai": {
            "default": "alloy",
            "source": "static",
            "voices": [
                {"id": "alloy"},
                {"id": "ballad", "models": ["gpt-4o-mini-tts"]},
            ],
        },
        "fish": {
            "source": "live",
            "voices": [
                {
                    "id": "03397b4c4be74759b72533b663fbd001",
                    "name": "Elon Musk(Noise reduction)",
                    "language": "en",
                },
                {"id": "another-fish-voice", "name": "Another Fish Voice"},
            ],
        },
    }


@pytest.fixture
def sample_catalog(sample_models, sample_voices):
    return {
        "version": 1,
        "fetched_at": 1.0,
        "models": sample_models,
        "voices": sample_voices,
    }
