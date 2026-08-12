from __future__ import annotations

from decimal import Decimal


class FakeClient:
    def __init__(self, models, voices, key="test-key"):
        self.models = models
        self.voices = voices
        self.key = key
        self.saved = None
        self.signup_email = None
        self.topup_amount = None

    def get_api_key(self):
        return self.key

    def preflight_credential_store(self):
        return None

    def start_signup(self, email):
        self.signup_email = email
        return {"success": True, "expiresIn": 300}

    def verify_signup(self, email, code):
        assert email == self.signup_email
        assert code == "123456"
        return {"apiKey": "new-secret-key"}

    def save_api_key(self, key):
        self.saved = key
        self.key = key

    def list_models(self, api_key=""):
        return self.models

    def list_voices(self, api_key=""):
        return {"voices": self.voices}

    def get_balance(self, api_key=""):
        return {
            "state": "ok",
            "paid_balance_usd": 2.5,
            "promotional_credits": 500000,
            "promotion_grants": [
                {
                    "name": "Welcome",
                    "remaining_usd": 0.5,
                    "eligible": True,
                    "expires_at": None,
                }
            ],
        }

    def create_topup(self, amount, api_key=""):
        self.topup_amount = amount
        return {"url": "https://pay.example/test", "expires_at": "soon"}


def make_command(speech_pkg, sample_models, sample_voices, key="test-key"):
    from hermes_speech_testpkg.catalog import CatalogStore
    from hermes_speech_testpkg.command import SpeechCommand
    from hermes_speech_testpkg.providers import AllModelsTTSProvider

    client = FakeClient(sample_models, sample_voices, key=key)
    catalog = CatalogStore(client)
    tts = AllModelsTTSProvider(client, catalog)
    return SpeechCommand(client, catalog, tts), client


def test_first_use_starts_signup(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices, key="")
    assert "account is required" in command.handle("")
    assert "/speech signup" in command.handle("")


def test_signup_verification_continues_to_tts_authors(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    command, client = make_command(speech_pkg, sample_models, sample_voices, key="")
    started = command.handle("signup person@example.com")
    assert "six-digit code" in started
    verified = command.handle("verify 123456")
    assert client.saved == "new-secret-key"
    assert "Choose a TTS model author" in verified
    assert "elevenlabs" in verified


def test_tts_author_model_voice_flow(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    authors = command.handle("tts model")
    assert "1. elevenlabs" in authors
    models = command.handle("tts model 1")
    assert "elevenlabs/eleven-turbo-v2-5" in models
    voices = command.handle("tts model 1")
    assert "TTS model set" in voices
    assert "Aria" in voices
    selected = command.handle("tts voice 1")
    assert "TTS setup is complete" in selected

    from hermes_speech_testpkg.settings import speech_status

    status = speech_status()
    assert status["tts_model"] == "elevenlabs/eleven-turbo-v2-5"
    assert status["tts_voice"] == "voice-a"
    assert status["tts_voice_provider"] == "elevenlabs"


def test_voice_search_uses_cached_metadata(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    command.handle("tts model elevenlabs/eleven-turbo-v2-5")

    help_text = command.handle("tts voice")
    assert "/speech tts voice search <query>" in help_text
    assert "Aria" in command.handle("tts voice search female")
    assert "Aria" in command.handle("tts voice aria")
    assert "Enter a voice name" in command.handle("tts voice search")


def test_control_center_displays_voice_name_instead_of_id(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    command.handle("tts model fish/s2-1-pro")
    command.handle("tts voice 03397b4c4be74759b72533b663fbd001")

    result = command.handle("")
    assert "Voice: Elon Musk(Noise reduction)" in result
    assert "03397b4c4be74759b72533b663fbd001" not in result
    assert settings.speech_status()["tts_voice"] == "03397b4c4be74759b72533b663fbd001"


def test_exact_alias_selects_model(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    result = command.handle("tts model elevenlabs/eleven_turbo_v2_5")
    assert "TTS model set to elevenlabs/eleven-turbo-v2-5" in result


def test_voice_menu_does_not_replace_model_number_snapshot(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    command.handle("tts model elevenlabs")
    command.handle("tts model 1")
    result = command.handle("tts model 2")
    assert "elevenlabs/eleven-multilingual-v2" in result


def test_model_change_clears_incompatible_voice(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_speech_testpkg import settings

    settings.set_tts_model("elevenlabs/eleven-turbo-v2-5")
    settings.set_tts_voice("voice-a", "elevenlabs")
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    result = command.handle("tts model openai/gpt-4o-mini-tts")
    assert "voice was incompatible" in result
    assert settings.speech_status()["tts_voice"] is None


def test_stt_author_model_flow(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    assert "1. openai" in command.handle("stt model")
    assert "openai/whisper-1" in command.handle("stt model 1")
    result = command.handle("stt model 1")
    assert "STT model set" in result

    from hermes_speech_testpkg.settings import speech_status

    status = speech_status()
    assert status["stt_model"] == "openai/whisper-1"
    assert status["stt_enabled"] is True


def test_advanced_settings_and_defaults(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    assert "1.25" in command.handle("advanced speed 1.25")
    assert "ja" in command.handle("advanced language ja")
    assert "updated" in command.handle("advanced prompt Hermes AllModels")

    from hermes_speech_testpkg import settings

    status = settings.speech_status()
    assert status["tts_speed"] == 1.25
    assert status["stt_language"] == "ja"
    assert status["stt_prompt"] == "Hermes AllModels"
    command.handle("advanced speed default")
    command.handle("advanced language default")
    command.handle("advanced prompt clear")
    status = settings.speech_status()
    assert status["tts_speed"] is None
    assert status["stt_language"] is None
    assert status["stt_prompt"] is None


def test_balance_and_topup(speech_pkg, hermes_home, sample_models, sample_voices):
    command, client = make_command(speech_pkg, sample_models, sample_voices)
    balance = command.handle("balance")
    assert "$2.50" in balance
    assert "$0.50" in balance
    assert "at most two decimal places" in command.handle("topup 4")
    link = command.handle("topup 25.50")
    assert "https://pay.example/test" in link
    assert client.topup_amount == Decimal("25.50")


def test_numeric_root_navigation(speech_pkg, hermes_home, sample_models, sample_voices):
    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    assert "TTS model" in command.handle("1")
    assert "Advanced speech tuning" in command.handle("5")


def test_update_commands_do_not_require_allmodels_account(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    class Updates:
        @staticmethod
        def format_check():
            return "Hermes Speech 0.2.0 is available"

        @staticmethod
        def format_update():
            return "Updated Hermes Speech from 0.1.0 to 0.2.0"

        @staticmethod
        def decorate_text(result):
            return result

    command, _ = make_command(speech_pkg, sample_models, sample_voices, key="")
    command.update_checker = Updates()
    assert "0.2.0 is available" in command.handle("update check")
    assert "Updated Hermes Speech" in command.handle("update")
    assert "Updated Hermes Speech" in command.handle("6")


def test_tts_test_returns_media_directive(
    speech_pkg, hermes_home, sample_models, sample_voices, tmp_path
):
    from hermes_speech_testpkg import settings

    command, _ = make_command(speech_pkg, sample_models, sample_voices)
    settings.set_tts_model("openai/gpt-4o-mini-tts")
    settings.set_tts_voice("alloy", "openai")

    class Provider:
        def synthesize(self, text, output_path, **kwargs):
            path = tmp_path / "test.mp3"
            path.write_bytes(b"audio")
            return str(path)

    command.tts_provider = Provider()
    result = command.handle("test Hello Hermes")
    assert "[[audio_as_voice]]" in result
    assert f"MEDIA:{tmp_path / 'test.mp3'}" in result


def test_config_updates_preserve_unrelated_settings(
    speech_pkg, hermes_home, sample_models, sample_voices
):
    from hermes_cli.config import read_raw_config, save_config
    from hermes_speech_testpkg import settings

    save_config({"display": {"skin": "nord"}, "custom": {"keep": True}}, strip_defaults=False)
    settings.set_stt_model("deepgram/nova-3")
    raw = read_raw_config()
    assert raw["display"]["skin"] == "nord"
    assert raw["custom"]["keep"] is True
    assert raw["stt"]["allmodels"]["model"] == "deepgram/nova-3"
