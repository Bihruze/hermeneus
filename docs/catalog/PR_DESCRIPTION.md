## What does this PR do?

Adds one catalog entry, `plugin-catalog/hermeneus.yaml`, for Hermeneus (https://github.com/Bihruze/hermeneus), pinned to `<FINAL_SHA>`. The entry is submitted by the repository owner (rule 5).

Hermeneus live-translates the audio playing on a Mac. It captures one app, or all system audio except itself, through a Core Audio process tap (macOS 14.2 and later, no driver). It streams that audio to ElevenLabs Scribe v3 Live for transcription with diarization, translates each committed line with the user's own Hermes model, and shows the subtitles in a Desktop pane, in a floating overlay above other apps, in a terminal, through a slash command, or on an opt-in 127.0.0.1 page for OBS. Spoken translation through ElevenLabs Flash v2.5 is opt-in. Language, mode (subtitles, both, voice), voice, output device and source change while a session runs. On older macOS it can read a virtual loopback device (BlackHole 2ch or VB-Cable) that the user installs. Listening covers 100 languages and speaking covers 32.

This entry lists macOS only. macOS is tested on real audio. The repository also holds a Windows backend that is implemented and reviewed against documentation, not yet run on a Windows machine; the entry does not list `windows`, so a catalog install refuses on Windows.

## Hermes surfaces used

| Surface | Where |
| --- | --- |
| `ctx.register_command("translate", ...)`: `/translate start\|set\|stop\|status\|voices\|overlay` (`overlay show\|hide\|status`) | `__init__.py:24-26`, `commands.py` |
| `ctx.register_cli_command("hermeneus", ...)`: `hermes hermeneus doctor\|devices\|run\|stop\|status\|set\|voices\|overlay\|overlay-page` | `__init__.py:27-29`, `commands.py:281-314` |
| `ctx.register_auxiliary_task("live_translate", ...)` and `ctx.llm.acomplete(..., task="live_translate")` | `__init__.py:22-23`, `glue.py` |
| `agent.plugin_llm.PluginLlm` for the dashboard routes, which have no `ctx` | `glue.py` |
| `requires_env: ELEVENLABS_API_KEY`, read with `agent.secret_scope.get_secret` (rule 11) | `plugin.yaml`, `glue.py` |
| `config_schema` settings under `plugins.entries.hermeneus.settings` (17 keys), read with `ctx.get_config`; `hermes hermeneus overlay-page on\|off` writes `overlay_page` and `overlay_page_port` with `ctx.set_config` | `plugin.yaml`, `glue.py`, `commands.py:94-101` |
| `providers.get_provider_profile` to name a missing Hermes extra before a session starts | `glue.py` |
| `plugins.plugin_storage.plugin_data_dir("hermeneus")` for the session socket, the lock and the overlay files | `glue.py`, `hermeneus_engine/runtime.py`, `hermeneus_engine/overlay/control.py` |
| `hermes_cli.config.load_config_readonly` for settings and `auxiliary.live_translate` outside `ctx` | `glue.py` |
| `ctx.on_unload` to stop a session the process owns | `__init__.py:30` |
| Dashboard backend routes at `/api/plugins/hermeneus/` (hidden tab): `GET status`, `GET lines`, `POST start`, `POST stop`, `POST settings`, `GET voices`, `GET overlay`, `POST overlay`, `POST overlay/apply`, `POST overlay/theme`, `GET devices`, `GET doctor`, WebSocket `events` | `dashboard/manifest.json`, `dashboard/plugin_api.py:65-179` |
| Desktop plugin SDK: one pane in `PANES_AREA`, one status bar item in `STATUSBAR_AREAS.right`, `ctx.i18n.register`; imports only `@hermes/plugin-sdk`, `react` and `react/jsx-runtime` | `desktop/plugin.js` |

The plugin registers no tools, hooks or middleware.

## Disclosures (rule 13)

Network calls to third-party services:

- Streams the captured audio to ElevenLabs Scribe v3 Live at `wss://api.elevenlabs.io/v1/speech-to-text/realtime` with the user's `ELEVENLABS_API_KEY`, for as long as a session runs, except that it stops sending after about 10 s of digital silence from the source and resumes with the first sound (session.py silence gate). ElevenLabs bills all streamed audio, including quiet audio that is not digital silence.
- With spoken translation on (off by default), sends the translated text to ElevenLabs text to speech at `wss://api.elevenlabs.io/v1/text-to-speech/<voice id>/multi-stream-input`.
- `GET https://api.elevenlabs.io/v2/voices` with the key in the `xi-api-key` header, for `hermes hermeneus voices`, `/translate voices`, `GET /voices` and the pane's voice picker (asked only when a mode with voice is chosen). Answers are cached in memory for 60 s. Code: `hermeneus_engine/tts/voice_catalog.py:31`, `:72-75`.
- Sends each committed segment, plus up to 3 earlier lines with their speaker labels and translations (`context_segments`, 0 to 10), to the user's configured model through `ctx.llm` under the plugin's own auxiliary task `live_translate`. No audio goes to the model. `hermes hermeneus doctor --test-model` sends one tiny extra request.
- From a chat platform (any surface outside `cli`, `tui`, `desktop` and `local`), `/translate start` and `/translate set --source` are refused by default with "Hermeneus captures audio on this Mac, so start it from the Mac (terminal or Hermes Desktop)." (`commands.py:31-48`, `:168`, `:199-203`; local set at `hermeneus_engine/overlay/control.py:39`). The `allow_remote_start` setting (default false) lets them through; with it on, a chat message can start capture of this Mac's audio and streaming to ElevenLabs with no action at the Mac. Status, stop and the other `set` changes work from anywhere. The overlay is refused from chat either way (`control.py:120-123`). The refusal covers slash commands only: `POST /start` from a Desktop connected to a remote backend (`dashboard/plugin_api.py` `_surface` returns `remote`) still starts a session.

Listening ports:

- None by default. The session socket is a Unix domain socket at `<HERMES_HOME>/plugin-data/hermeneus/run/sock`, mode 0600 inside a 0700 directory. The Desktop pane uses routes mounted on Hermes's own server.
- Off by default: with `overlay_page` on (`hermes hermeneus overlay-page on`), the session owner process listens on 127.0.0.1:47821 (`overlay_page_port`; a busy port is an error, no fallback port) while the session runs, on its own thread. Loopback only, never 0.0.0.0. It serves one self-contained page and a Server-Sent Events stream of subtitle lines for OBS browser sources and second screens. Every path needs a per-session random token (`secrets.token_urlsafe(24)`), requests without it get 403, the Host header must be `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding), GET and HEAD only, no CORS headers, no control routes, a hash-based Content-Security-Policy. It stops with the session, on plugin unload and with the process. Code: `hermeneus_engine/overlay_page.py`, page: `hermeneus_engine/overlay_page.html`.

Long-running background processes and threads:

- One session at a time, run in-process on its own thread and event loop.
- Capture worker: the process tap runs one Python worker subprocess per session (`python -m hermeneus_engine.capture._tap_worker`, the interpreter Hermes runs on). It stops with the session (stdin closed, then SIGTERM, then SIGKILL, 5 seconds apart), when its stdin closes because Hermes exited, on SIGTERM, SIGINT or SIGHUP, on plugin unload and at interpreter exit. It gets an allowlisted environment (PATH, HOME, TMPDIR, LANG, LC_ALL, LC_CTYPE, plus its own PYTHONPATH and PYTHONUNBUFFERED) and no secrets (`hermeneus_engine/subproc.py:30`, `:60-71`; started at `hermeneus_engine/capture/process_tap.py:107-109`).
- Overlay window: while the user has it open, the floating overlay is a separate Python process (`python -m hermeneus_engine.overlay --data-dir <dir>`, `hermeneus_engine/overlay/control.py:168-169`, started at `:196-197`) in its own process session. It reads subtitles from the Unix session socket, opens no network connection and no port, needs no macOS permission and never takes keyboard focus. It exits when the session stops, on `overlay hide` and on SIGTERM, and refuses to start without a desktop session. It gets the same allowlisted environment plus the import path (`child_env`, `control.py:172-176`, which is `subproc.python_env`), so no API key, token or other secret of the Hermes process reaches it.
- Doctor test player: `hermes hermeneus doctor` and the pane's Doctor tab (each time it opens or reruns) start one short-lived player subprocess (`python -c <player script>`, allowlisted environment, `hermeneus_engine/capture/tap_doctor.py:79-109`, started at `:86-87`). It plays silence, then a 440 Hz test tone for about 0.5 s, and is killed before the tap stops. The tone plays on BlackHole or VB-Cable when one is installed (-23 dBFS, not audible), otherwise on the default output at -53.5 dBFS while the tap mutes the player (`CATapMutedWhenTapped`; whether that is fully silent is not measured). `--no-audio-check` skips the check. The first run shows the System Audio Recording prompt and waits up to 30 s.

Capture scope and permissions:

- The process tap captures one chosen app with its helper processes, or all system audio except the Hermeneus processes. The fallback reads the virtual device the user routes audio to. Microphones are not captured.
- Needs the macOS System Audio Recording permission for the app that runs Hermes (Terminal or another terminal app for the CLI, Hermes for Desktop). The fallback needs the Microphone permission for that app, because a virtual device is an input device.

Shell commands. Every helper process starts through `hermeneus_engine/subproc.py`: programs by absolute path (`subproc.py:25-26`, refused otherwise at `:74-77`), argument lists, no shell, and the minimal environment by default (`:47-57`, `:80-88`). `tests/test_subproc.py` checks that no other module calls `subprocess` directly.

- `/usr/sbin/system_profiler SPAudioDataType -json` (read only) to read audio device names and check that spoken output cannot feed back into the capture (`hermeneus_engine/tts/routing.py:108`, `hermeneus_engine/capture/virtual_device.py:673`).
- `/bin/ps -o ppid=,comm= -p <pid>` (read only, at most 32 steps up the parent chain) to name the app in the permission message (`hermeneus_engine/capture/process_tap.py:419-444`, the call at `:427`).

Files and reads outside the plugin's data:

- `plugin-data/hermeneus/run/lock` holds the owner pid, owner kind, start time and socket path, and is emptied when the session stops.
- `plugin-data/hermeneus/overlay/` (0700) holds five files: `lock` (the overlay pid while it runs), `settings.json` (overlay settings), `theme.json` (the Desktop theme colours, radius and font the pane sent through `POST /overlay/theme`, read by the overlay and the overlay page), `positions.json` (place and width per display) and `overlay.log` (the overlay's stderr, replaced at each start). No subtitle text (`hermeneus_engine/overlay/control.py:53-71`).
- Subtitles stay in memory (last 200 lines). No audio and no transcript is written to disk.
- Reads outside the plugin's data: Hermes's own config through `hermes_cli.config.load_config_readonly`, and the audio device and process lists macOS gives through Core Audio.

Third-party software and credentials:

- BlackHole 2ch or VB-Cable, for the fallback only, installed by the user from the official source. Nothing is bundled, downloaded or installed by the plugin.
- `ELEVENLABS_API_KEY` only, read through `agent.secret_scope.get_secret`; never stored or printed by the plugin. The doctor prints "set (value not shown)".
- No telemetry. No self-updater (rule 3).

## Screenshots

Placeholders until the images are committed at the pinned sha:

- `<SCREENSHOT: docs/screenshots/pane-subtitles.png>` Desktop pane, Subtitles tab, during a session.
- `<SCREENSHOT: docs/screenshots/pane-doctor.png>` Desktop pane, Doctor tab.
- `<SCREENSHOT: docs/screenshots/overlay.png>` the floating subtitle overlay above a full-screen video.
- `<SCREENSHOT: docs/screenshots/overlay-page-obs.png>` the overlay page as an OBS browser source.
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
796 passed, 1 warning
ℹ pass 68
ℹ fail 0
ok: no em dashes
ok: no binaries tracked
ok: no attribution lines
Validation passed.
```

On a Mac with macOS 14.2 or later:

1. `hermes plugins install hermeneus` and `hermes config set ELEVENLABS_API_KEY <key>`.
2. `hermes hermeneus doctor` lists each check with a fix for any gap. It plays a short quiet test tone to check capture; `hermes hermeneus doctor --no-audio-check` skips it.
3. Play speech in any app, then `hermes hermeneus run --to tr`. Allow the System Audio Recording prompt on first use. Translated lines print in the terminal; Ctrl+C stops the session.
4. In Hermes Desktop, turn on Hermeneus in Capabilities > Plugins, open the pane, pick a source, a language and a mode, and press Start.
5. `hermes hermeneus overlay show` floats the subtitles above other apps; `hermes hermeneus overlay hide` closes it.
