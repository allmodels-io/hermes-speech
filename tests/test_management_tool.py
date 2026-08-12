from __future__ import annotations

import json
from decimal import Decimal

from test_command import FakeClient


def make_tool(speech_pkg, sample_models, sample_voices, *, key="test-key"):
    from hermes_speech_testpkg.catalog import CatalogStore
    from hermes_speech_testpkg.management_tool import AllModelsSpeechManagementTool
    from hermes_speech_testpkg.providers import AllModelsTTSProvider

    client = FakeClient(sample_models, sample_voices, key=key)
    catalog = CatalogStore(client)
    tts = AllModelsTTSProvider(client, catalog)
    return AllModelsSpeechManagementTool(client, catalog, tts), client


def call(tool, action, **kwargs):
    return json.loads(tool.handle({"action": action, **kwargs}))


def test_management_requires_existing_account(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices, key="")
    result = call(tool, "get_status")
    assert result["success"] is False
    assert result["error"] == "account_required"
    assert result["next_action"] == "load_configure_allmodels_speech"


def test_status_uses_human_readable_voice_name(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    settings.set_tts_model("fish/s2-1-pro")
    settings.set_tts_voice("03397b4c4be74759b72533b663fbd001", "fish")
    settings.set_stt_model("soniox/stt-async-v5")
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    result = call(tool, "get_status")
    assert result["success"] is True
    assert result["tts"]["voice"] == "Elon Musk(Noise reduction)"
    assert result["tts"]["active"] is True
    assert result["stt"]["active"] is True


def test_model_discovery_and_exact_selection(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    authors = call(tool, "list_model_authors", capability="tts")
    assert authors["authors"] == ["elevenlabs", "openai", "fish"]

    models = call(tool, "find_models", capability="tts", author="fish")
    assert [item["id"] for item in models["models"]] == ["fish/s2-1-pro"]

    partial = call(tool, "select_model", capability="tts", model_id="fish/s2")
    assert partial["error"] == "model_not_found"

    selected = call(
        tool,
        "select_model",
        capability="tts",
        model_id="fish/s2.1-pro",
    )
    assert selected["selected_model"] == "fish/s2-1-pro"
    assert selected["compatible_voices"][0]["name"] == "Elon Musk(Noise reduction)"

    stt = call(
        tool,
        "select_model",
        capability="stt",
        model_id="soniox/stt-async-v5",
    )
    assert stt["selected_model"] == "soniox/stt-async-v5"

    from hermes_speech_testpkg import settings

    status = settings.speech_status()
    assert status["tts_model"] == "fish/s2-1-pro"
    assert status["stt_model"] == "soniox/stt-async-v5"


def test_voice_search_selection_and_incompatible_rejection(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    sample_voices["fish"]["voices"][0]["description"] = (
        "Deep confident public-speaking voice"
    )
    settings.set_tts_model("fish/s2-1-pro")
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    found = call(tool, "find_voices", query="deep confident")
    assert len(found["voices"]) == 1
    assert found["voices"][0]["name"] == "Elon Musk(Noise reduction)"
    assert found["voices"][0]["description"].startswith("Deep confident")

    rejected = call(tool, "select_voice", voice_id="not-compatible")
    assert rejected["error"] == "incompatible_voice"
    assert rejected["valid_voices"][0]["name"] == "Elon Musk(Noise reduction)"

    selected = call(
        tool,
        "select_voice",
        voice_id="03397b4c4be74759b72533b663fbd001",
        voice_provider="fish",
    )
    assert selected["selected_voice"]["name"] == "Elon Musk(Noise reduction)"
    assert settings.speech_status()["tts_voice_provider"] == "fish"


def test_voice_discovery_can_target_an_unselected_model_or_search_globally(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    settings.set_tts_model("openai/gpt-4o-mini-tts")
    settings.set_tts_voice("alloy", "openai")
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    fish = call(tool, "find_voices", model_id="fish/s2-1-pro")
    assert fish["scope"] == "model"
    assert fish["model"] == "fish/s2-1-pro"
    assert fish["voices"][0]["name"] == "Elon Musk(Noise reduction)"
    assert fish["voices"][0]["compatible_models"] == ["fish/s2-1-pro"]
    assert settings.speech_status()["tts_model"] == "openai/gpt-4o-mini-tts"

    global_result = call(tool, "find_voices", query="Elon noise")
    assert global_result["scope"] == "all_models"
    assert global_result["model"] is None
    assert global_result["voices"][0]["name"] == "Elon Musk(Noise reduction)"
    assert "fish/s2-1-pro" in global_result["voices"][0]["compatible_models"]


def test_model_change_clears_incompatible_voice(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    settings.set_tts_model("elevenlabs/eleven-turbo-v2-5")
    settings.set_tts_voice("voice-a", "elevenlabs")
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    result = call(
        tool,
        "select_model",
        capability="tts",
        model_id="openai/gpt-4o-mini-tts",
    )
    assert result["previous_voice_cleared"] is True
    assert settings.speech_status()["tts_voice"] is None


def test_balance_and_topup_link(speech_pkg, hermes_home, sample_models, sample_voices):
    tool, client = make_tool(speech_pkg, sample_models, sample_voices)
    balance = call(tool, "get_balance")
    assert balance["spendable_paid_balance_usd"] == "2.50"
    assert balance["promotional_balance_usd"] == "0.50"
    assert balance["promotional_grants"][0]["name"] == "Welcome"

    invalid = call(tool, "create_topup_link", amount_usd=4)
    assert invalid["error"] == "invalid_amount"
    topup = call(tool, "create_topup_link", amount_usd=25.50)
    assert topup["url"] == "https://pay.example/test"
    assert topup["amount_usd"] == "25.50"
    assert client.topup_amount == Decimal("25.5")


def test_advanced_settings(speech_pkg, hermes_home, sample_models, sample_voices):
    from hermes_speech_testpkg import settings

    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)
    assert call(tool, "set_speed", speed=1.25)["speed"] == 1.25
    assert call(tool, "set_language", language="pt-BR")["language_override"] == "pt-BR"
    assert (
        call(tool, "set_prompt", text="Keep AllModels exact")["prompt_configured"]
        is True
    )

    assert call(tool, "set_speed", use_default=True)["behavior"] == "default"
    assert call(tool, "set_language", use_default=True)["language_override"] is None
    assert call(tool, "set_prompt", clear=True)["prompt_configured"] is False

    status = settings.speech_status()
    assert status["tts_speed"] is None
    assert status["stt_language"] is None
    assert status["stt_prompt"] is None


def test_tts_test_returns_media_directive(
    speech_pkg, hermes_home, sample_models, sample_voices, tmp_path
):
    from hermes_speech_testpkg import settings

    settings.set_tts_model("openai/gpt-4o-mini-tts")
    settings.set_tts_voice("alloy", "openai")
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)

    class Provider:
        def synthesize(self, text, output_path, **kwargs):
            path = tmp_path / "managed-test.mp3"
            path.write_bytes(b"audio")
            return str(path)

    tool.tts_provider = Provider()
    result = call(tool, "test_tts", text="Hello from management")
    assert result["success"] is True
    assert result["media_directive"].endswith(f"MEDIA:{tmp_path / 'managed-test.mp3'}")


def test_voice_preview_uses_explicit_pair_without_changing_configuration(
    speech_pkg, hermes_home, sample_models, sample_voices, tmp_path
):
    from hermes_cli.config import read_raw_config
    from hermes_speech_testpkg import settings

    settings.set_tts_model("fish/s2-1-pro")
    settings.set_tts_voice("03397b4c4be74759b72533b663fbd001", "fish")
    settings.set_tts_speed(1.4)
    settings.set_stt_model("soniox/stt-async-v5")
    before = read_raw_config()
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)
    captured = {}

    class Provider:
        def synthesize(self, text, output_path, **kwargs):
            captured["text"] = text
            captured["output_path"] = output_path
            captured.update(kwargs)
            path = tmp_path / "one-off preview.mp3"
            path.write_bytes(b"audio")
            return str(path)

    tool.tts_provider = Provider()
    result = call(
        tool,
        "preview_voice",
        text="This is only a preview.",
        model_id="openai/gpt-4o-mini-tts",
        voice_id="alloy",
        voice_provider="openai",
        speed=0.9,
    )

    assert result["success"] is True
    assert result["configuration_changed"] is False
    assert result["preview"]["model"] == "openai/gpt-4o-mini-tts"
    assert result["preview"]["voice"] == "alloy"
    assert result["media_directive"].endswith(
        f'MEDIA:"{tmp_path / "one-off preview.mp3"}"'
    )
    assert captured["model"] == "openai/gpt-4o-mini-tts"
    assert captured["voice"] == "alloy"
    assert captured["voice_provider"] == "openai"
    assert captured["speed"] == 0.9
    assert captured["format"] == "mp3"
    assert read_raw_config() == before


def test_voice_preview_rejects_incompatible_pair_without_writing_config(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_cli.config import read_raw_config

    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)
    before = read_raw_config()
    result = call(
        tool,
        "preview_voice",
        text="Should not synthesize",
        model_id="openai/gpt-4o-mini-tts",
        voice_id="03397b4c4be74759b72533b663fbd001",
        voice_provider="fish",
    )
    assert result["error"] == "incompatible_voice"
    assert read_raw_config() == before


def test_management_schema_excludes_signup_and_api_keys(speech_pkg):
    from hermes_speech_testpkg.management_tool import MANAGEMENT_TOOL_SCHEMA

    properties = MANAGEMENT_TOOL_SCHEMA["parameters"]["properties"]
    actions = properties["action"]["enum"]
    assert not any("signup" in action or "verify" in action for action in actions)
    assert "preview_voice" in actions
    assert "api_key" not in properties
