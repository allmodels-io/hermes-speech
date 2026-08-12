from __future__ import annotations

import queue
import threading
from types import SimpleNamespace


class FakeClient:
    def __init__(self, key="secret-key"):
        self.key = key

    def get_api_key(self):
        return self.key


class FakeCatalog:
    def __init__(self, catalog=None):
        self.catalog = catalog

    def cached(self):
        return self.catalog


def test_streaming_tts_uses_openai_pcm_chunks(
    speech_pkg, hermes_home, monkeypatch, sample_catalog, caplog
):
    from hermes_speech_testpkg.client import OPENAI_BASE_URL
    from hermes_speech_testpkg.streaming import AllModelsStreamingTTSProvider

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            captured["response_closed"] = True

        @staticmethod
        def iter_bytes():
            yield b"pcm-one"
            yield b"pcm-two"

    class Create:
        @staticmethod
        def create(**kwargs):
            captured["request"] = kwargs
            return Response()

    class OpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.audio = SimpleNamespace(
                speech=SimpleNamespace(
                    with_streaming_response=SimpleNamespace(create=Create.create)
                )
            )

        def close(self):
            captured["client_closed"] = True

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    provider = AllModelsStreamingTTSProvider(
        {
            "provider": "allmodels",
            "model": "fish/s2-1-pro",
            "voice": "fish-voice",
            "speed": 1.25,
        },
        {"voice_provider": "fish"},
        client=FakeClient(),
        catalog=FakeCatalog(sample_catalog),
    )

    with caplog.at_level("INFO"):
        assert list(provider.stream("Hello from streaming.")) == [b"pcm-one", b"pcm-two"]
    assert captured["client"]["base_url"] == OPENAI_BASE_URL
    assert captured["request"]["model"] == "fish/s2-1-pro"
    assert captured["request"]["voice"] == "fish-voice"
    assert captured["request"]["input"] == "Hello from streaming."
    assert captured["request"]["response_format"] == "pcm"
    assert captured["request"]["speed"] == 1.25
    assert captured["request"]["extra_body"] == {"provider": "fish"}
    assert captured["response_closed"] is True
    assert captured["client_closed"] is True
    assert "secret-key" not in repr(captured["request"])
    assert "AllModels TTS path: streaming PCM" in caplog.text
    assert "AllModels TTS stream complete" in caplog.text
    assert "chunks=2" in caplog.text
    assert "bytes=14" in caplog.text
    assert "Hello from streaming" not in caplog.text
    assert "fish-voice" not in caplog.text
    assert "secret-key" not in caplog.text


def test_streaming_tts_rejects_non_streaming_model_binding(
    speech_pkg, hermes_home, sample_catalog, caplog
):
    import pytest

    from hermes_speech_testpkg.streaming import AllModelsStreamingTTSProvider

    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="does not advertise streaming"):
            AllModelsStreamingTTSProvider(
                {
                    "provider": "allmodels",
                    "model": "openai/gpt-4o-mini-tts",
                    "voice": "alloy",
                },
                {"voice_provider": "openai"},
                client=FakeClient(),
                catalog=FakeCatalog(sample_catalog),
            )
    assert "AllModels TTS path: synchronous fallback" in caplog.text
    assert "reason=not_advertised" in caplog.text
    assert "alloy" not in caplog.text


def test_streaming_tts_registration_resolves_allmodels_and_falls_back(
    speech_pkg, hermes_home, sample_catalog
):
    from hermes_speech_testpkg.streaming import register_allmodels_streaming_provider
    from tools.tts_streaming import _REGISTRY, resolve_streaming_provider

    previous = _REGISTRY.get("allmodels")
    try:
        registered = register_allmodels_streaming_provider(
            FakeClient(), FakeCatalog(sample_catalog)
        )
        resolved = resolve_streaming_provider(
            {
                "provider": "allmodels",
                "model": "fish/s2-1-pro",
                "voice": "fish-voice",
                "allmodels": {"voice_provider": "fish"},
            }
        )
        assert isinstance(resolved, registered)
        assert resolved.sample_rate == 24000

        unsupported = resolve_streaming_provider(
            {
                "provider": "allmodels",
                "model": "openai/gpt-4o-mini-tts",
                "voice": "alloy",
                "allmodels": {"voice_provider": "openai"},
            }
        )
        assert unsupported is None
    finally:
        if previous is None:
            _REGISTRY.pop("allmodels", None)
        else:
            _REGISTRY["allmodels"] = previous


def test_voice_compatibility_follows_delivery_platform(
    speech_pkg, hermes_home, monkeypatch
):
    from hermes_speech_testpkg.providers import AllModelsTTSProvider

    provider = AllModelsTTSProvider(FakeClient(), FakeCatalog())
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    assert provider.voice_compatible is False

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    assert provider.voice_compatible is True

    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "desktop")
    assert provider.voice_compatible is False


def test_voice_mode_sentence_pipeline_synthesizes_and_plays_each_sentence(
    speech_pkg, hermes_home, monkeypatch
):
    """Exercise Hermes' real /voice TTS sync pipeline with AllModels.

    This is the path used after ``/voice on`` + ``/voice tts``: Hermes splits
    the assistant response into sentences, dispatches each through the active
    plugin provider, and hands each generated file to its standard player.
    """
    from agent import tts_registry
    from hermes_speech_testpkg import settings
    from hermes_speech_testpkg.providers import AllModelsTTSProvider
    from tools import tts_tool, voice_mode

    settings.set_tts_model("fish/s2-1-pro")
    settings.set_tts_voice("voice-id", "fish")
    provider = AllModelsTTSProvider(FakeClient(), FakeCatalog())
    monkeypatch.setitem(tts_registry._providers, "allmodels", provider)
    monkeypatch.setattr(
        "hermes_cli.plugins._ensure_plugins_discovered",
        lambda force=False: None,
    )
    monkeypatch.setattr(
        "tools.tts_streaming.resolve_streaming_provider",
        lambda *args, **kwargs: None,
    )
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)

    requests = []
    played = []

    class Response:
        def __init__(self, text):
            self.text = text

        def stream_to_file(self, path):
            path.write_bytes(b"ID3" + self.text.encode("utf-8"))

    class OpenAI:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(
                speech=SimpleNamespace(create=self.create_speech)
            )

        @staticmethod
        def create_speech(**kwargs):
            requests.append(kwargs)
            return Response(kwargs["input"])

        def close(self):
            pass

    def play_audio_file(path):
        with open(path, "rb") as handle:
            played.append((path, handle.read()))
        return True

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    monkeypatch.setattr(voice_mode, "play_audio_file", play_audio_file)
    monkeypatch.setattr(
        tts_tool,
        "_convert_to_opus",
        lambda path: (_ for _ in ()).throw(
            AssertionError("local voice mode must not convert MP3 to Opus")
        ),
    )

    text_queue = queue.Queue()
    # The agent loop feeds streamed text deltas as they arrive. A completed
    # sentence delta is dispatched immediately while later text is still being
    # generated, which is what enables synthesis/playback overlap.
    text_queue.put("First complete sentence is ready. ")
    text_queue.put("Second complete sentence is ready.")
    text_queue.put(None)
    stop = threading.Event()
    done = threading.Event()

    tts_tool.stream_tts_to_speaker(text_queue, stop, done)

    assert done.is_set()
    assert [request["input"] for request in requests] == [
        "First complete sentence is ready.",
        "Second complete sentence is ready.",
    ]
    assert all(request["model"] == "fish/s2-1-pro" for request in requests)
    assert all(request["voice"] == "voice-id" for request in requests)
    assert all(request["extra_body"] == {"provider": "fish"} for request in requests)
    assert len(played) == 2
    assert all(path.endswith(".mp3") for path, _audio in played)
    assert [audio for _path, audio in played] == [
        b"ID3First complete sentence is ready.",
        b"ID3Second complete sentence is ready.",
    ]


def test_tts_provider_sends_model_voice_provider_and_speed(
    speech_pkg, hermes_home, monkeypatch, tmp_path, caplog
):
    from hermes_speech_testpkg import settings
    from hermes_speech_testpkg.providers import AllModelsTTSProvider

    settings.set_tts_model("openai/gpt-4o-mini-tts")
    settings.set_tts_voice("alloy", "openai")
    captured = {}

    class Response:
        def stream_to_file(self, path):
            path.write_bytes(b"audio")

    class OpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.audio = SimpleNamespace(
                speech=SimpleNamespace(create=lambda **kw: captured.update(request=kw) or Response())
            )

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    provider = AllModelsTTSProvider(FakeClient(), FakeCatalog())
    output = tmp_path / "voice.mp3"
    with caplog.at_level("INFO"):
        result = provider.synthesize(
            "hello", str(output), model="openai/gpt-4o-mini-tts", voice="alloy", speed=1.2
        )
    assert result == str(output)
    assert output.read_bytes() == b"audio"
    assert captured["request"]["model"] == "openai/gpt-4o-mini-tts"
    assert captured["request"]["voice"] == "alloy"
    assert captured["request"]["speed"] == 1.2
    assert captured["request"]["extra_body"] == {"provider": "openai"}
    assert "secret-key" not in repr(captured["request"])
    assert "AllModels TTS path: synchronous file" in caplog.text
    assert "AllModels TTS synchronous complete" in caplog.text
    assert "hello" not in caplog.text
    assert "alloy" not in caplog.text
    assert "secret-key" not in caplog.text


def test_tts_provider_allows_one_off_voice_provider_override(
    speech_pkg, hermes_home, monkeypatch, tmp_path
):
    from hermes_speech_testpkg import settings
    from hermes_speech_testpkg.providers import AllModelsTTSProvider

    settings.set_tts_model("fish/s2-1-pro")
    settings.set_tts_voice("fish-voice", "fish")
    captured = {}

    class Response:
        def stream_to_file(self, path):
            path.write_bytes(b"audio")

    class OpenAI:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(
                speech=SimpleNamespace(
                    create=lambda **kw: captured.update(request=kw) or Response()
                )
            )

        def close(self):
            pass

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    provider = AllModelsTTSProvider(FakeClient(), FakeCatalog())
    provider.synthesize(
        "preview",
        str(tmp_path / "preview.mp3"),
        model="openai/gpt-4o-mini-tts",
        voice="alloy",
        voice_provider="openai",
    )

    assert captured["request"]["model"] == "openai/gpt-4o-mini-tts"
    assert captured["request"]["voice"] == "alloy"
    assert captured["request"]["extra_body"] == {"provider": "openai"}
    status = settings.speech_status()
    assert status["tts_model"] == "fish/s2-1-pro"
    assert status["tts_voice"] == "fish-voice"
    assert status["tts_voice_provider"] == "fish"


def test_stt_provider_forwards_language_and_optional_prompt(
    speech_pkg, hermes_home, monkeypatch, tmp_path, caplog
):
    from hermes_speech_testpkg import settings
    from hermes_speech_testpkg.providers import AllModelsTranscriptionProvider

    settings.set_stt_model("openai/whisper-1")
    settings.set_stt_prompt("Hermes AllModels")
    captured = {}

    class OpenAI:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(
                transcriptions=SimpleNamespace(
                    create=lambda **kw: captured.update(request=kw) or SimpleNamespace(text="hello")
                )
            )

        def close(self):
            pass

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    provider = AllModelsTranscriptionProvider(FakeClient(), FakeCatalog())
    with caplog.at_level("INFO"):
        result = provider.transcribe(str(audio), model="openai/whisper-1", language="ja")
    assert result == {"success": True, "transcript": "hello", "provider": "allmodels"}
    assert captured["request"]["language"] == "ja"
    assert captured["request"]["prompt"] == "Hermes AllModels"
    assert "AllModels STT path: synchronous file upload" in caplog.text
    assert "AllModels STT synchronous complete" in caplog.text
    assert "Hermes AllModels" not in caplog.text
    assert "hello" not in caplog.text


def test_provider_errors_redact_api_key(speech_pkg, hermes_home, monkeypatch, tmp_path):
    from hermes_speech_testpkg import settings
    from hermes_speech_testpkg.providers import AllModelsTranscriptionProvider

    settings.set_stt_model("openai/whisper-1")

    class OpenAI:
        def __init__(self, **kwargs):
            def fail(**kw):
                raise RuntimeError("request rejected for secret-key")

            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=fail))

        def close(self):
            pass

    monkeypatch.setattr("openai.OpenAI", OpenAI)
    audio = tmp_path / "sample.wav"
    audio.write_bytes(b"wav")
    result = AllModelsTranscriptionProvider(FakeClient(), FakeCatalog()).transcribe(str(audio))
    assert result["success"] is False
    assert "secret-key" not in result["error"]
    assert "[REDACTED]" in result["error"]
