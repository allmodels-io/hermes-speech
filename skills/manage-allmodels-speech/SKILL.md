---
name: manage-allmodels-speech
description: Manage an existing AllModels speech account, Hermes speech configuration, and the Hermes Speech plugin installation. Use when the user wants to inspect or change TTS/STT models, find or select voices by name or qualities, preview a voice without changing saved settings, check balance or promotional grants, create a top-up link, test configured speech, change TTS speed, override STT language, set or clear an STT prompt, check for a Hermes Speech update, or explicitly update the plugin. Prefer this skill for an unqualified voice-management request when Hermes is already configured with AllModels. Do not use for signup, verification, first-time setup, replacing an account, an explicit request for Edge or another named provider, or merely toggling voice mode.
---

# Manage AllModels Speech

Use `allmodels_speech_manage` for every operation. Do not call AllModels directly, edit Hermes configuration manually, or invoke `/speech` through the terminal.

## Tool access

Use `allmodels_speech_manage` directly when it is visible. Hermes may defer plugin tools behind progressive tool discovery. If the tool is not directly visible:

1. Call `tool_describe` with `name: allmodels_speech_manage` to load its schema.
2. Call `tool_call` with `name: allmodels_speech_manage` and the required arguments.

Do not substitute generic `text_to_speech`, terminal configuration edits, Edge, or invented catalog entries. If neither the direct tool nor `tool_describe`/`tool_call` can find `allmodels_speech_manage`, report that the `hermes-speech` plugin must be installed and enabled, then stop.

## Account and status

- Call `get_status` when the current selection or advanced settings matter.
- If any action returns `account_required`, stop management and load `configure-allmodels-speech` for first-time setup. Never perform signup in this workflow.
- Call `get_balance` for paid balance, promotional balance, and applicable grants.
- For a top-up, obtain the amount, then call `create_topup_link`. Return the secure URL without opening it. Creating a link does not itself charge the user.

## Plugin updates

- Call `check_update` for a read-only request to check the latest stable GitHub Release.
- Call `update_plugin` only when the user explicitly asks to install or apply an update. Do not infer installation permission from a status check or an automatic `plugin_update` notice.
- Report whether the plugin was current or updated. After a successful update, tell the user to restart Hermes; do not attempt hot reload.
- If any ordinary tool result contains `plugin_update`, mention the available version briefly after completing the user's requested operation. Do not interrupt signup or replace the requested result with the notice.

## Models

1. Determine whether the request concerns `tts` or `stt`.
2. Call `list_model_authors` when the user has not named an author. Present the returned authors without inventing options.
3. Call `find_models` with an exact author or partial query. Present up to the returned limit.
4. Call `select_model` only with an exact canonical ID or alias. Never guess from partial results.
5. After selecting a TTS model, present its compatible voices and continue to voice selection if the user asked for complete TTS configuration. STT selection is complete after the model is saved.

## Voices

- Call `find_voices` with no query to list voices for the active TTS model. Pass `model_id` to inspect a different model without selecting it. Pass descriptive terms as `query` without a model to search names, descriptions, language, gender, provider, and IDs across all synchronous TTS models; use each result's `compatible_models` for selection or preview.
- Present human-readable names and useful descriptions, language, gender, or provider. Do not show opaque voice IDs unless requested for technical details.
- Call `select_voice` with the exact ID and provider returned by `find_voices`. Report the human-readable selected name.
- For a one-off sample, call `preview_voice` with text and an exact compatible model ID, voice ID, and provider returned by `find_voices`. Include its `media_directive` exactly. State that it was a preview and do not claim the saved TTS model or voice changed.
- If the active model changes and its previous voice is cleared, tell the user and help select a compatible replacement.

## Testing and advanced settings

- Call `test_tts` only to test the currently configured model and voice. Include the returned `media_directive` exactly so Hermes delivers the audio. Use `preview_voice` for any other model or voice.
- Call `set_speed` with `speed` from 0.25 through 4, or `use_default: true` to remove the override.
- Call `set_language` with a language tag such as `en`, `ja`, or `pt-BR`, or `use_default: true` to restore Hermes' inherited language resolution.
- Call `set_prompt` with `text`, or `clear: true` to remove the transcription hint.

## Guardrails

- Never request, display, or manipulate the AllModels API key.
- Never run Git commands or modify plugin files directly; use `check_update` or `update_plugin`.
- Never use this skill for signup, verification, or account replacement.
- Never select a partial model or incompatible voice match.
- Keep output format unchanged; the plugin preserves the configured format or normal MP3 default.
- `/voice on`, `/voice tts`, and `/voice off` control voice mode; this skill configures and tests the provider.
