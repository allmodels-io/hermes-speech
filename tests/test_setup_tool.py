from __future__ import annotations

import json

from test_command import FakeClient


def make_tool(speech_pkg, sample_models, sample_voices, *, key="test-key"):
    from hermes_speech_testpkg.catalog import CatalogStore
    from hermes_speech_testpkg.setup_tool import AllModelsSpeechSetupTool

    client = FakeClient(sample_models, sample_voices, key=key)
    catalog = CatalogStore(client)
    return AllModelsSpeechSetupTool(client, catalog), client


def test_status_requests_email_without_requiring_credentials(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices, key="")
    result = json.loads(tool.handle({"action": "status"}))
    assert result == {
        "authenticated": False,
        "configured": False,
        "instruction": (
            "Stop setup work and ask only for the user's email. Do not configure "
            "Edge/local speech and do not install packages."
        ),
        "needs": "email",
        "next_action": "start_signup",
        "success": True,
        "workflow_skill": "configure-allmodels-speech",
    }


def test_signup_verification_saves_key_and_configures_balanced_defaults(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    tool, client = make_tool(speech_pkg, sample_models, sample_voices, key="")
    started = json.loads(
        tool.handle({"action": "start_signup", "email": "Person@example.com"})
    )
    assert started["signup_started"] is True
    assert started["email"] != "person@example.com"
    assert started["verification_email"] == {
        "sender": "noreply@allmodels.io",
        "subject_contains": "Your Allmodels verification code",
        "subject_example": "675693 — Your Allmodels verification code",
        "code_pattern": "six digits at the start of the subject",
        "prefer_newest_after_signup": True,
    }
    assert "already has authorized email search access" in started["instruction"]
    assert "Do not require an exact subject match" in started["instruction"]

    verified_raw = tool.handle(
        {
            "action": "verify_signup",
            "email": "person@example.com",
            "code": "123456",
        }
    )
    verified = json.loads(verified_raw)
    assert verified["success"] is True
    assert verified["account_created"] is True
    assert verified["credentials_saved"] is True
    assert verified["selections"] == {
        "tts_model": "fish/s2-1-pro",
        "tts_voice": "Elon Musk(Noise reduction)",
        "tts_voice_provider": "fish",
        "stt_model": "soniox/stt-async-v5",
    }
    assert client.saved == "new-secret-key"
    assert "new-secret-key" not in verified_raw
    assert "03397b4c4be74759b72533b663fbd001" not in verified_raw

    status_result = json.loads(tool.handle({"action": "status"}))
    assert status_result["selections"]["tts_voice"] == "Elon Musk(Noise reduction)"
    assert "03397b4c4be74759b72533b663fbd001" not in json.dumps(status_result)

    from hermes_speech_testpkg import settings

    status = settings.speech_status()
    assert status["tts_provider"] == "allmodels"
    assert status["stt_provider"] == "allmodels"
    assert status["stt_enabled"] is True


def test_default_setup_is_atomic_and_preserves_advanced_settings(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_cli.config import save_config

    save_config(
        {
            "model": {"default": "example/model"},
            "tts": {"output_format": "wav", "speed": 1.25},
            "stt": {
                "language": "ja",
                "allmodels": {"prompt": "Keep product names exact"},
            },
        },
        strip_defaults=False,
    )
    tool, _ = make_tool(speech_pkg, sample_models, sample_voices)
    result = json.loads(tool.handle({"action": "configure_defaults"}))
    assert result["success"] is True

    from hermes_speech_testpkg import settings

    config = settings.current()
    assert config["model"]["default"] == "example/model"
    assert config["tts"]["output_format"] == "wav"
    assert config["tts"]["speed"] == 1.25
    assert config["stt"]["language"] == "ja"
    assert config["stt"]["allmodels"]["prompt"] == "Keep product names exact"


def test_setup_tool_schema_never_accepts_an_api_key(speech_pkg):
    from hermes_speech_testpkg.setup_tool import SETUP_TOOL_SCHEMA

    properties = SETUP_TOOL_SCHEMA["parameters"]["properties"]
    assert "api_key" not in properties
    assert "code" in properties
