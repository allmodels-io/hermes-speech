"""Hermes Speech — native AllModels TTS/STT plugin."""

from __future__ import annotations

from pathlib import Path

__all__ = ["register"]


def register(ctx) -> None:
    """Register AllModels providers, commands, tool, and setup skill."""
    # Import lazily so manifest/test discovery can inspect the root module
    # without importing Hermes' provider registries or optional runtime clients.
    from .catalog import CatalogStore
    from .client import AllModelsClient
    from .command import SpeechCommand
    from .management_tool import MANAGEMENT_TOOL_SCHEMA, AllModelsSpeechManagementTool
    from .providers import AllModelsTTSProvider, AllModelsTranscriptionProvider
    from .skill_discovery import ensure_skill_discovery
    from .setup_tool import SETUP_TOOL_SCHEMA, AllModelsSpeechSetupTool
    from .streaming import register_allmodels_streaming_provider
    from .update_checker import PluginUpdateChecker

    ensure_skill_discovery(Path(__file__).resolve().parent / "skills")

    client = AllModelsClient()
    catalog = CatalogStore(client)
    tts = AllModelsTTSProvider(client, catalog)
    stt = AllModelsTranscriptionProvider(client, catalog)
    updates = PluginUpdateChecker()
    register_allmodels_streaming_provider(client, catalog)
    command = SpeechCommand(client, catalog, tts, update_checker=updates)
    setup_tool = AllModelsSpeechSetupTool(client, catalog, update_checker=updates)
    management_tool = AllModelsSpeechManagementTool(
        client,
        catalog,
        tts,
        update_checker=updates,
    )

    ctx.register_tts_provider(tts)
    ctx.register_transcription_provider(stt)
    ctx.register_command(
        "speech",
        handler=command.handle,
        description="Configure AllModels speech, signup, voices, and billing.",
        args_hint="[tts|stt|account|test|advanced]",
    )
    ctx.register_tool(
        name="allmodels_speech_setup",
        toolset="allmodels_speech",
        schema=SETUP_TOOL_SCHEMA,
        handler=setup_tool.handle,
        description="Conversational signup and default TTS/STT configuration for AllModels.",
        emoji="🔊",
    )
    ctx.register_tool(
        name="allmodels_speech_manage",
        toolset="allmodels_speech",
        schema=MANAGEMENT_TOOL_SCHEMA,
        handler=management_tool.handle,
        description="Manage AllModels models, voices, previews, balance, tests, and tuning.",
        emoji="🎛️",
    )
