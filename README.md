# Hermes Speech

`hermes-speech` is a native [Hermes Agent](https://github.com/NousResearch/hermes-agent)
plugin for AllModels TTS and STT. It registers `allmodels` as a Hermes text-to-speech
and transcription provider, adds a guided `/speech` command, and bundles an
agent-facing setup tool and skill.

The plugin exposes two normal Hermes skills:

- `configure-allmodels-speech` for signup and automatic first-time defaults
- `manage-allmodels-speech` for authenticated model, voice, balance, top-up,
  testing, and advanced-setting changes

## Requirements

- Hermes Agent 0.20.0 or later
- No additional Python packages; the plugin uses Hermes' bundled `openai` and
  `httpx` libraries

## Install

Install and enable the plugin directly from GitHub:

```bash
hermes plugins install allmodels-io/hermes-speech --enable
```

Restart a running Hermes CLI or gateway after installation.

## Development

Run the focused suite from the repository root with Hermes' Python environment:

```bash
python -m pytest -q tests
python -m ruff check .
```

## Usage

The easiest setup is conversational. Tell Hermes:

```text
Set up AllModels speech for me.
```

With the plugin enabled, unqualified setup requests such as `Set up speech`,
`Configure TTS and STT`, or `Get voice working` match the bundled skill by its
normal Hermes skill description. Hermes then loads that skill with `skill_view`
and follows the AllModels workflow. There is no per-message intent hook or fixed
sentence list. Explicit requests for local, offline, Edge, Whisper, or another
named provider remain with Hermes' built-in setup.

Hermes checks the current setup, asks for an email and the single-use code only
when needed, and installs balanced TTS/STT defaults. By default it selects
`fish/s2-1-pro` with Fish voice `Elon Musk(Noise reduction)`
and `soniox/stt-async-v5`, with catalog-ordered fallbacks if a preferred entry
is unavailable.

The setup tool never requires an API key as an argument and never returns one.

After setup, management is conversational too. Requests such as `Find a warmer
voice`, `Switch my STT model`, `Check my AllModels balance`, or `Create a $25
top-up link` discover `manage-allmodels-speech`. Its agent-facing tool uses the
same client, catalog, provider, and settings implementation as the `/speech`
interface; it does not perform signup.

Voice search can span all synchronous TTS models. A conversational
`preview_voice` action validates and synthesizes an exact model/voice pair as a
temporary MP3 without changing the configured TTS model, voice, speed, or
format, then returns audio through Hermes' normal `MEDIA:` delivery.

Run `/speech`. If `ALLMODELS_API_KEY` is not configured, Hermes immediately
starts AllModels email signup:

```text
/speech signup you@example.com
/speech verify 123456
```

After verification, setup continues with the TTS author picker. Model setup is
guided as author → model → voice for TTS and author → model for STT.

Useful direct commands:

```text
/speech tts model
/speech tts voice search <name, language, gender, or provider>
/speech stt model
/speech balance
/speech topup 25
/speech test Hello from Hermes
/speech advanced speed 1.1
/speech advanced language ja
/speech advanced prompt Product names: Hermes, AllModels
/speech update check
/speech update
```

`/voice` remains the Hermes command for enabling or disabling voice mode.
`/speech` configures which AllModels models and voice Hermes uses.

For conversational speech, enable Hermes' normal pipeline with `/voice on`
followed by `/voice tts`. Hermes splits streamed replies into sentences,
synthesizes each sentence through the registered AllModels provider, and plays
them in order. When the selected catalog binding supports streaming, the plugin
uses Hermes' bundled OpenAI client to yield raw PCM chunks through Hermes'
streaming-TTS pipeline. Other models automatically retain the synchronous,
sentence-pipelined path. Local CLI/TUI/desktop file output keeps Hermes'
requested MP3; messaging gateways that require native voice bubbles use
Ogg/Opus.

## Configuration

The plugin adds its bundled `skills/` directory to the active profile's
`skills.external_dirs`, which makes the workflow part of Hermes' normal skill
index. The skills discover the plugin's setup and management tools directly or
through Hermes' deferred tool search. Speech setup writes only its relevant keys
in `config.yaml`. The API key is stored in the profile's protected `.env` as
`ALLMODELS_API_KEY`; it is never displayed after signup.

Catalogs refresh automatically in the background. There is no manual refresh
command.

Hermes Speech checks the repository's latest stable GitHub Release in the
background when `/speech` or an agent-facing plugin tool is used. The result is
cached for 24 hours and an available release is mentioned at most weekly until
installed. The checker sends no account, speech, model, voice, or installation
identifier data. Disable automatic checks with:

```yaml
plugins:
  hermes-speech:
    update_check: false
```

Automatic checks only notify. `/speech update` or an explicit conversational
request such as `Update Hermes Speech` performs the update and then asks for a
Hermes restart. Linked development installs, non-Git copies, unexpected Git
remotes, and checkouts with local changes are never modified automatically.
