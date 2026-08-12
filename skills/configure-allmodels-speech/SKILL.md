---
name: configure-allmodels-speech
description: Set up AllModels as Hermes' native text-to-speech and speech-to-text provider. Use when a user explicitly asks for AllModels or generically asks to set up, configure, connect, enable, or get speech, voice, TTS, STT, or transcription working without naming another provider. Do not use for explicit local, offline, Edge, Whisper, built-in, or other named-provider requests, or for merely toggling voice mode.
---

# Configure AllModels Speech

Use `allmodels_speech_setup` to complete setup conversationally. Keep questions short and ask only for information the next tool action requires.

This workflow owns unqualified speech setup while the plugin is enabled. Do not configure Hermes' Edge/local providers, install speech packages, or run generic speech setup in parallel.

## Tool access

Use `allmodels_speech_setup` directly when it is visible. Hermes may defer plugin tools behind progressive tool discovery. If the tool is not directly visible:

1. Call `tool_describe` with `name: allmodels_speech_setup` to load its schema.
2. Call `tool_call` with `name: allmodels_speech_setup` and the required arguments.

Do not substitute terminal commands or another speech provider. If neither the direct tool nor `tool_describe`/`tool_call` can find `allmodels_speech_setup`, report that the `hermes-speech` plugin must be installed and enabled, then stop.

## Workflow

1. Call `allmodels_speech_setup` with `action: status`.
2. If `next_action` is `start_signup`, stop all other work and ask only for the user's email address unless they already supplied it. Then call `start_signup` with that email.
3. After signup starts, check whether this session already has an authorized email-search integration. If it does, use its existing tools to find the newest message received after signup from `noreply@allmodels.io` whose subject contains `Your Allmodels verification code`. Do not require an exact subject: it normally looks like `675693 — Your Allmodels verification code`. Extract the leading six digits from that newest matching subject and call `verify_signup` with the original email and code. Do not initiate new email authorization. If email access is unavailable or no recent matching subject is found, stop and ask only for the code. The code is single-use and may be supplied in normal conversation.
4. Verification saves the credential and configures balanced TTS, voice, and STT defaults automatically. If it returns `catalog_initializing`, retry `configure_defaults` once.
5. If status says the account is authenticated but setup is incomplete, call `configure_defaults` without asking more questions.
6. When `next_action` is `done`, summarize the selected TTS model, human-readable voice name, and STT model. Never show the stored voice ID unless the user explicitly requests technical details. Tell the user that `/voice on` and `/voice tts` enable Hermes' normal spoken-reply pipeline.
7. If a tool result contains `plugin_update`, mention it briefly after completing the current setup step. Never interrupt an email or verification-code request with the notice, and never install an update without an explicit user request.

## Guardrails

- Never ask the user to paste an API key. Use email signup or an already saved credential.
- Do not display or repeat a returned API key.
- Do not replace rejected saved credentials until the user confirms they want to replace the account and provides an email.
- Do not change speech format, language, prompt, speed, or unrelated Hermes settings during default setup.
- Do not install `faster-whisper`, `edge-tts`, or any other dependency during this workflow.
- Use email access only to retrieve the matching recent verification code; do not summarize or expose unrelated messages.
- For later model, voice, billing, testing, or advanced tuning requests, load `manage-allmodels-speech` and use `allmodels_speech_manage`.
