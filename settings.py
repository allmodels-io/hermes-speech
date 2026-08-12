"""Atomic, profile-aware Hermes speech configuration helpers."""

from __future__ import annotations

import os
import threading
from copy import deepcopy
from typing import Any, Dict, Optional

_CONFIG_LOCK = threading.RLock()


def _section(parent: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


def _mutate(mutator) -> None:
    from hermes_cli.config import read_raw_config, save_config

    with _CONFIG_LOCK:
        raw = read_raw_config()
        config = deepcopy(raw) if isinstance(raw, dict) else {}
        mutator(config)
        save_config(config, strip_defaults=False)


def current() -> Dict[str, Any]:
    from hermes_cli.config import load_config

    config = load_config()
    return config if isinstance(config, dict) else {}


def speech_status() -> Dict[str, Any]:
    cfg = current()
    tts = cfg.get("tts") if isinstance(cfg.get("tts"), dict) else {}
    stt = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
    allmodels = stt.get("allmodels") if isinstance(stt.get("allmodels"), dict) else {}
    tts_allmodels = tts.get("allmodels") if isinstance(tts.get("allmodels"), dict) else {}
    return {
        "tts_provider": tts.get("provider"),
        "tts_model": tts.get("model"),
        "tts_voice": tts.get("voice"),
        "tts_speed": tts.get("speed"),
        "tts_output_format": tts.get("output_format"),
        "tts_voice_provider": tts_allmodels.get("voice_provider"),
        "stt_enabled": stt.get("enabled", True),
        "stt_provider": stt.get("provider"),
        "stt_model": allmodels.get("model"),
        "stt_language": allmodels.get("language"),
        "stt_prompt": allmodels.get("prompt"),
        "stt_inherited_language": inherited_stt_language(stt),
    }


def inherited_stt_language(stt: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if stt is None:
        cfg = current()
        value = cfg.get("stt")
        stt = value if isinstance(value, dict) else {}
    provider = stt.get("allmodels") if isinstance(stt.get("allmodels"), dict) else {}
    for candidate in (
        provider.get("language"),
        stt.get("language"),
        os.environ.get("HERMES_LOCAL_STT_LANGUAGE"),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def set_tts_model(model: str, *, clear_voice: bool = False) -> None:
    def change(config: Dict[str, Any]) -> None:
        tts = _section(config, "tts")
        tts["provider"] = "allmodels"
        tts["model"] = model
        if clear_voice:
            tts.pop("voice", None)
            _section(tts, "allmodels").pop("voice_provider", None)

    _mutate(change)


def set_tts_voice(voice: str, provider: str) -> None:
    def change(config: Dict[str, Any]) -> None:
        tts = _section(config, "tts")
        tts["provider"] = "allmodels"
        tts["voice"] = voice
        _section(tts, "allmodels")["voice_provider"] = provider

    _mutate(change)


def set_tts_speed(speed: Optional[float]) -> None:
    def change(config: Dict[str, Any]) -> None:
        tts = _section(config, "tts")
        tts["provider"] = "allmodels"
        if speed is None:
            tts.pop("speed", None)
        else:
            tts["speed"] = float(speed)

    _mutate(change)


def set_stt_model(model: str) -> None:
    def change(config: Dict[str, Any]) -> None:
        stt = _section(config, "stt")
        stt["enabled"] = True
        stt["provider"] = "allmodels"
        _section(stt, "allmodels")["model"] = model

    _mutate(change)


def set_stt_language(language: Optional[str]) -> None:
    def change(config: Dict[str, Any]) -> None:
        stt = _section(config, "stt")
        stt["enabled"] = True
        stt["provider"] = "allmodels"
        target = _section(stt, "allmodels")
        if language is None:
            target.pop("language", None)
        else:
            target["language"] = language

    _mutate(change)


def set_stt_prompt(prompt: Optional[str]) -> None:
    def change(config: Dict[str, Any]) -> None:
        stt = _section(config, "stt")
        stt["enabled"] = True
        stt["provider"] = "allmodels"
        target = _section(stt, "allmodels")
        if prompt is None:
            target.pop("prompt", None)
        else:
            target["prompt"] = prompt

    _mutate(change)


def configure_defaults(
    *,
    tts_model: str,
    tts_voice: str,
    tts_voice_provider: str,
    stt_model: str,
) -> None:
    """Activate both providers in one atomic config update.

    Existing output format and advanced speed/language/prompt settings are
    deliberately preserved when setup is run again.
    """

    def change(config: Dict[str, Any]) -> None:
        tts = _section(config, "tts")
        tts["provider"] = "allmodels"
        tts["model"] = tts_model
        tts["voice"] = tts_voice
        _section(tts, "allmodels")["voice_provider"] = tts_voice_provider

        stt = _section(config, "stt")
        stt["enabled"] = True
        stt["provider"] = "allmodels"
        _section(stt, "allmodels")["model"] = stt_model

    _mutate(change)
