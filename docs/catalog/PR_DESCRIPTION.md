## What does this PR do?

Adds one catalog entry, `plugin-catalog/hermeneus.yaml`, for Hermeneus (https://github.com/Bihruze/hermeneus), pinned to `<FINAL_SHA>`. The entry is submitted by the repository owner (rule 5).

Hermeneus live-translates the audio playing on a Mac. It captures one app, or all system audio except itself, through a Core Audio process tap (macOS 14.2 and later, no driver). It streams that audio to ElevenLabs Scribe v3 Live for transcription with diarization, translates each committed line with the user's own Hermes model, and shows the subtitles in a Desktop pane, in a terminal or through a slash command. Spoken translation through ElevenLabs Flash v2.5 is opt-in. On older macOS it can read a virtual loopback device (BlackHole 2ch or VB-Cable) that the user installs. Listening covers 100 languages and speaking covers 32. This release is macOS only.

## Hermes surfaces used

| Surface | Where |
| --- | --- |
| `ctx.register_command("translate", ...)`: `/translate start\|stop\|status` | `__init__.py`, `commands.py` |
| `ctx.register_cli_command("hermeneus", ...)`: `hermes hermeneus doctor\|devices\|run\|stop\|status` | `__init__.py`, `commands.py` |
| `ctx.register_auxiliary_task("live_translate", ...)` and `ctx.llm.acomplete(..., task="live_translate")` | `__init__.py`, `glue.py` |
| `agent.plugin_llm.PluginLlm` for the dashboard routes, which have no `ctx` | `glue.py` |
| `requires_env: ELEVENLABS_API_KEY`, read with `agent.secret_scope.get_secret` (rule 11) | `plugin.yaml`, `glue.py` |
| `config_schema` settings under `plugins.entries.hermeneus.settings`, read with `ctx.get_config` | `plugin.yaml`, `glue.py` |
| `plugins.plugin_storage.plugin_data_dir("hermeneus")` for the session socket and lock | `glue.py`, `hermeneus_engine/runtime.py` |
| `hermes_cli.config.load_config_readonly` for settings and `auxiliary.live_translate` outside `ctx` | `glue.py` |
| `ctx.on_unload` to stop a session the process owns | `__init__.py` |
| Dashboard backend routes at `/api/plugins/hermeneus/` (hidden tab): `status`, `lines`, `start`, `stop`, `devices`, `doctor`, `events` (WebSocket) | `dashboard/manifest.json`, `dashboard/plugin_api.py` |
| Desktop plugin SDK: one pane in `PANES_AREA`, one status bar item in `STATUSBAR_AREAS.right`, `ctx.i18n.register`; imports only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime` | `desktop/plugin.js` |

The plugin registers no tools, hooks or middleware.

## Disclosures (rule 13)

- Network: streams the captured audio to ElevenLabs Scribe v3 Live at `wss://api.elevenlabs.io/v1/speech-to-text/realtime` with the user's `ELEVENLABS_API_KEY`, for as long as a session runs. ElevenLabs bills all streamed audio, silence included.
- Network: with spoken translation on, sends the translated text to ElevenLabs text to speech at `wss://api.elevenlabs.io/v1/text-to-speech/<voice id>/multi-stream-input`. Off by default.
- Model: sends each committed segment, plus up to 3 earlier lines with their speaker labels and translations (`context_segments`, 0 to 10), to the user's configured model through `ctx.llm` under the plugin's own auxiliary task `live_translate`. No audio goes to the model.
- Capture scope: the process tap captures one chosen app with its helper processes, or all system audio except the Hermeneus processes. The fallback reads the virtual device the user routes audio to. Microphones are not captured.
- Permission: needs the macOS System Audio Recording permission for the app that runs Hermes (Terminal or another terminal app for the CLI, Hermes for Desktop). The fallback needs the Microphone permission for that app, because a virtual device is an input device.
- Background process: the process tap runs one Python worker subprocess per session (`python -m hermeneus_engine.capture._tap_worker`, the interpreter Hermes runs on). It stops with the session (stdin closed, then SIGTERM, then SIGKILL, 5 seconds apart), when its stdin closes because Hermes exited, on SIGTERM, SIGINT or SIGHUP, on plugin unload and at interpreter exit. It inherits the Hermes process environment.
- Background thread: one session at a time, run in-process on its own thread and event loop.
- IPC: a Unix domain socket at `<HERMES_HOME>/plugin-data/hermeneus/run/sock`, mode 0600 inside a 0700 directory. No TCP port is opened. The Desktop pane uses routes mounted on Hermes's own server.
- Shell commands: `system_profiler SPAudioDataType -json` (read only, argv list) to read audio device names and check that spoken output cannot feed back into the capture.
- Files: a lock file `run/lock` next to the socket holds the owner pid, owner kind, start time and socket path, and is emptied when the session stops. Subtitles stay in memory (last 200 lines). No audio and no transcript is written to disk.
- Third-party software: BlackHole 2ch or VB-Cable, for the fallback only, installed by the user from the official source. Nothing is bundled, downloaded or installed by the plugin.
- Credentials: `ELEVENLABS_API_KEY` only, read through `agent.secret_scope.get_secret`; never stored or printed by the plugin. The doctor prints "set (value not shown)".
- No telemetry. No self-updater (rule 3).

## Screenshots

Placeholders until the images are committed at the pinned sha:

- `<SCREENSHOT: docs/screenshots/pane-subtitles.png>` Desktop pane, Subtitles tab, during a session.
- `<SCREENSHOT: docs/screenshots/pane-doctor.png>` Desktop pane, Doctor tab.
- `<SCREENSHOT: docs/screenshots/statusbar.png>` status bar item.
- `<SCREENSHOT: docs/screenshots/cli-run.png>` `hermes hermeneus run --to tr` in a terminal.
- `<SCREENSHOT: docs/screenshots/permission-prompt.png>` the macOS System Audio Recording prompt.
- `<SCREENSHOT: docs/banner.png>` the 2:1 banner for `image:`.

## How to test

From a checkout of https://github.com/Bihruze/hermeneus at `<FINAL_SHA>`. The result lines below were measured on the pre-release branch; they are re-run at `<FINAL_SHA>` before this PR opens.

```bash
hermes plugins validate . --install-deps 2>&1 | tail -1
```

```text
Validation passed.
```

Structural check of the entry, from a directory holding only `hermeneus.yaml`:

```bash
python3 scripts/validate_plugin_catalog.py <dir> 2>&1 | tail -1
```

```text
OK: 1 file(s) valid
```

The plugin's own suite (pytest, the desktop pane tests under `node --test`, and the em dash, binary and attribution scans):

```bash
scripts/check.sh 2>&1 | grep -E "passed|failed|^ℹ (pass|fail)|^ok:|FAIL|Validation"
```

```text
361 passed, 1 warning
ℹ pass 17
ℹ fail 0
ok: no em dashes
ok: no binaries tracked
ok: no attribution lines
Validation passed.
```

On a Mac with macOS 14.2 or later:

1. `hermes plugins install hermeneus` and `hermes config set ELEVENLABS_API_KEY <key>`.
2. `hermes hermeneus doctor` lists each check with a fix for any gap.
3. Play speech in any app, then `hermes hermeneus run --to tr`. Allow the System Audio Recording prompt on first use. Translated lines print in the terminal; Ctrl+C stops the session.
4. In Hermes Desktop, open the Hermeneus pane, pick a source and a language, and press Start.
