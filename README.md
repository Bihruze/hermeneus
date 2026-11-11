# Hermeneus

Live translation for Hermes.

Hermeneus listens to the audio playing on your Mac and shows a translated subtitle for each line of speech. It captures one app or all system audio, sends the audio to ElevenLabs Scribe v3 Live for transcription, and translates each finished line with the model you already use in Hermes. Subtitles appear in a Hermes Desktop pane, in a floating overlay above other apps, in your terminal, on a local page for OBS when you turn it on, or as the reply to a slash command. Spoken translation is opt-in: when you turn it on, ElevenLabs Flash v2.5 reads each translated line aloud on an output device you choose.

This release is tested on macOS with real audio. Windows support is a preview: it is implemented and reviewed against documentation, and has not yet run on a Windows machine (see [Windows (preview)](#windows-preview)). Linux support is planned.

## What it does

1. A capture backend reads the audio. On macOS 14.2 and later this is a Core Audio process tap, which needs no driver. On older macOS, or when you choose it, Hermeneus reads a virtual loopback device such as BlackHole 2ch or VB-Cable that you install yourself.
2. The audio is streamed to ElevenLabs Scribe v3 Live (`wss://api.elevenlabs.io/v1/speech-to-text/realtime`) with speaker diarization and multilingual mode on. Scribe returns partial and committed transcripts, a language code for each segment, and a speaker label.
3. Each committed segment is translated by your Hermes model through the plugin LLM API (`ctx.llm`) under the auxiliary task `live_translate`. The request carries the segment and up to 3 earlier lines as context, so names and terms stay consistent.
4. The translated line appears in every place that is showing the session: the Desktop pane, the subtitle overlay, the overlay page, `hermes hermeneus run` in a terminal, and `/translate status`.
5. With spoken translation on, the translated line goes to ElevenLabs Flash v2.5 over its multi-stream-input WebSocket and plays on the output device you picked. The mode decides what you get: subtitles only, subtitles and voice, or voice only.

## Requirements

- macOS 14.2 or later for the process tap. macOS 13 runs only with the virtual device fallback.
- Hermes 0.23.1 or later (`requires_hermes: ">=0.23.1"` in `plugin.yaml`).
- Python 3.12 or later. Hermes itself allows 3.11, but the plugin needs numpy 2.5.4 (the version Hermes v0.23.1 pins in its `voice` extra), and numpy 2.5.4 declares `Requires-Python >=3.12`, so it does not install on 3.11.
- An ElevenLabs account and API key. Create one at https://elevenlabs.io/app/settings/api-keys.
- A model configured in Hermes. A small, fast model for the `live_translate` auxiliary task keeps subtitles close to the speech (see [Configuration](#configuration)).
- Hermes Desktop, only if you want the subtitle pane.
- For the fallback only: BlackHole 2ch (https://github.com/ExistentialAudio/BlackHole) or VB-Cable (https://vb-audio.com/Cable/), installed by you.

Python dependencies (`websockets`, `sounddevice`, `numpy`, on macOS `pyobjc-framework-CoreAudio` and `pyobjc-framework-Cocoa`, and on Windows only `comtypes`, `pycaw` and `psutil`) are declared in `pyproject.toml`. Hermes asks for your consent and installs them when you install or enable the plugin.

## Install

From the Hermes plugin catalog, once the entry is listed:

```bash
hermes plugins install hermeneus
```

Straight from the repository:

```bash
hermes plugins install Bihruze/hermeneus
```

For local testing from a clone on your Mac (Hermes clones the committed `HEAD` of that clone and prints a security warning for `file://` sources):

```bash
git clone https://github.com/Bihruze/hermeneus.git
hermes plugins install file://$PWD/hermeneus
```

If Hermes installed the plugin disabled, turn it on:

```bash
hermes plugins enable hermeneus
```

Then add your ElevenLabs key. Run this in your own terminal so the key does not pass through a chat:

```bash
hermes config set ELEVENLABS_API_KEY <key>
```

Hermes Desktop copies the plugin's desktop half (`desktop/plugin.js`) into `~/.hermes/desktop-plugins/` when the plugin is installed. Restart the gateway after enabling the plugin so the pane can reach its backend routes.

To remove it:

```bash
hermes plugins remove hermeneus
```

## First run

In Hermes Desktop, turn on Hermeneus in Capabilities > Plugins. Desktop installs the pane and the status bar item switched off, as it does for the desktop half of every plugin package, and shows neither until the plugin is on.

Run the doctor first:

```bash
hermes hermeneus doctor
```

It checks the macOS version, which capture backends can run, the System Audio Recording permission, the Multi-Output Device for the fallback, your ElevenLabs key (it prints "set (value not shown)", never the value), the translation model and the overlay page. Each line that is not `ok` comes with a `fix:` line. `hermes hermeneus doctor --json` prints the same checks as JSON.

### The doctor's capture check and its test tone

On a Mac where the process tap is available, the doctor proves capture with real audio:

1. It starts a small player process (the Python that runs Hermes, with `sounddevice`) that holds an output stream open and plays silence.
2. It taps that one process through the real capture worker, with the tap set to mute the process on its output device.
3. It plays a 440 Hz test tone for about half a second, stops the player before the tap, and checks that the tone arrived.

Where the tone plays:

- When an output device named BlackHole, VB-Cable, VB-Audio, Loopback Audio or Soundflower is installed, the tone plays there at -23 dBFS. Nobody hears that device.
- Otherwise the tone plays on your default output at -53.5 dBFS, a very quiet level, while the tap mutes it. The doctor's own result says you may have heard it briefly. Whether the mute makes it fully silent has not been measured.

The first run of this check on a Mac shows the macOS System Audio Recording prompt (see below), and the check waits up to 30 seconds for your answer. The Doctor tab in the Desktop pane runs the same check, test tone included, each time you open the tab or press **Run checks again**.

Two flags change what the doctor does:

| Flag | Effect |
| --- | --- |
| `--no-audio-check` | Skips the capture check. No player starts, no tone plays and no permission prompt appears; the permission line then reads as a warning. |
| `--test-model` | Also sends one tiny request to the `live_translate` model, which spends a few tokens with your model provider. |

### The System Audio Recording permission

The first time Hermeneus captures audio through a process tap, in the doctor's check or in your first session, macOS asks for the System Audio Recording permission. The prompt names the app that runs Hermes: your terminal app (for example Terminal or iTerm) when you use the CLI, or Hermes when you use Desktop.

1. Run `hermes hermeneus doctor`, or start a session, for example `hermes hermeneus run --to tr`.
2. When macOS shows the prompt, click **Allow**.
3. If you clicked **Don't Allow** or no prompt appeared, open **System Settings > Privacy & Security > Screen & System Audio Recording**. Under **System Audio Recording Only**, turn on the app that runs Hermes. The full Screen & System Audio Recording entry also works, but Hermeneus needs only the audio part.
4. Quit and reopen that app, then run the doctor or start the session again.

A session waits up to 30 seconds for capture to start while the prompt is open. Without the permission macOS delivers silence instead of audio; the doctor and a running session both report that with the fix above.

Apps cannot read this permission directly: macOS keeps it in a database that apps can read only by asking for it or with Full Disk Access. The doctor therefore proves it with the test tone above. With `--no-audio-check` it shows the permission line as a warning with the fix above.

> Screenshot placeholder (W16): docs/screenshots/permission-prompt.png, the macOS System Audio Recording prompt.
>
> Screenshot placeholder (W16): docs/screenshots/permission-settings.png, Screen & System Audio Recording in System Settings with the terminal app turned on.

## Usage

Every way of starting a session needs a target language. Pass it with `--to` or set `target_language` once in the settings. Language codes are ISO 639-1 or ISO 639-3 codes such as `tr`, `de` or `fil`.

### CLI

```bash
hermes hermeneus doctor            # what this Mac needs, with a fix for each gap
hermes hermeneus devices           # audio sources and output devices
hermes hermeneus run --to tr       # live subtitles in this terminal
hermes hermeneus status            # the session state
hermes hermeneus set --mode both   # change the running session (see Changing a running session)
hermes hermeneus voices            # the voices on your ElevenLabs account
hermes hermeneus stop              # stop the running session
hermes hermeneus overlay show      # float the subtitles above other apps (see Subtitle overlay)
hermes hermeneus overlay hide      # close the overlay; the session keeps running
hermes hermeneus overlay status    # whether the overlay is open
hermes hermeneus overlay-page on   # subtitles on a 127.0.0.1 page for OBS (off by default)
```

`hermes hermeneus run` flags:

| Flag | Meaning |
| --- | --- |
| `--to LANG` | Language to translate into, for example `tr` or `de`. |
| `--source NAME` | Audio source to capture, as `hermes hermeneus devices` lists it. A unique part of the name is enough, for example `--source chrome`. |
| `--speak` | Speak the translation as well. Subtitles are always shown. |
| `--device OUTPUT` | Output device for spoken translation. |
| `--voice VOICE_ID` | ElevenLabs voice id for spoken translation. |
| `--json` | Print every event as one JSON line. |

`doctor`, `devices`, `status`, `set`, `voices`, `stop`, `overlay` and `overlay-page` also take `--json`. `doctor` also takes `--no-audio-check` and `--test-model` (see [First run](#first-run)). `voices` also takes `--refresh`.

`run` prints one translated line per segment, prefixed with the speaker's number in order of first appearance, for example `[Speaker 1] ...`, the same numbering the pane uses. A line that could not be translated is printed in the original language with the reason. Status changes go to standard error. Press Ctrl+C to stop a session that this `run` started.

When a session is already running (started from Desktop or by a slash command), `run` attaches to it and prints its lines. Its start flags are ignored. Ctrl+C then detaches and the session keeps running; `hermes hermeneus stop` ends it.

Without `--source`, Hermeneus captures all system audio except its own output on a Mac that offers the process tap. On the fallback it uses the virtual device when exactly one is installed, and otherwise asks you to pass `--source`.

### Changing a running session

`hermes hermeneus set` (and `/translate set` in a Hermes session) changes the running session without restarting it. It answers at once and changes nothing when it refuses.

| Flag | Meaning |
| --- | --- |
| `--to LANG` | New subtitle language. |
| `--mode subtitles\|both\|voice` | Subtitles only, subtitles and voice, or voice only. In voice only the status reads "subtitles hidden"; in this build the pane, the overlay and the overlay page still show the lines. |
| `--speak on\|off` | Turn spoken translation on or off. |
| `--voice VOICE_ID` | Another ElevenLabs voice. |
| `--device OUTPUT` | Another output device for spoken translation. |
| `--source NAME` | Another audio source, as `hermes hermeneus devices` lists it. |

Spoken translation needs an output device. Pass `--device` together with `--mode both`, `--mode voice` or `--speak on` when none is set yet.

### Spoken translation output

`hermes hermeneus devices`, `GET /devices` and the pane's **Output device** picker list only the outputs that are safe and audible. Hermeneus leaves out every output that would feed the speech back into the capture source, and it also leaves out virtual devices such as BlackHole and VB-Cable, because a person who picks one from the list hears nothing (`hermeneus_engine/tts/routing.py`, `safe_output_devices`).

A virtual device is still accepted when you name it, for example `--device "BlackHole 2ch"` or `output_device: BlackHole 2ch`. Use that to send the spoken translation into another app, such as OBS or a call app that reads BlackHole as its microphone. You hear the speech only if that app plays the device to your speakers or headphones. The feedback check still applies: on the fallback, a session that captures BlackHole 2ch refuses to speak into BlackHole 2ch. With the process tap any output is safe, because Hermeneus leaves its own process out of the capture.

### Voices

`hermes hermeneus voices` lists the voices on your ElevenLabs account with their ids and the languages ElevenLabs has verified for Flash v2.5. It asks ElevenLabs (`GET https://api.elevenlabs.io/v2/voices`) and keeps the answer for 60 seconds; `--refresh` asks again. Pass a voice id with `--voice`, or set `voice_id`. The default voice is Talia (see [Configuration](#configuration)).

### Overlay page for OBS and second screens

```bash
hermes hermeneus overlay-page on                # serve the page during every session
hermes hermeneus overlay-page on --port 47822   # the same on another port
hermes hermeneus overlay-page                   # show the page address while a session runs
hermes hermeneus overlay-page off
```

With the page on, the process that runs the session serves live subtitles at `http://127.0.0.1:47821/<token>/` while the session runs. The command, `run` and the doctor print the full address. Add it in OBS as a Browser source, or open it in a browser on this Mac for a second screen. The page background is transparent. It shows the newest lines, each as soon as it is heard and then replaced in place by its translation.

The page uses the shared overlay settings `overlay_max_lines`, `overlay_text_size`, `overlay_show_original`, `overlay_opacity` and `overlay_show_speaker`. Query parameters override them per source: `max_lines` (1 to 3), `text_size` (14 to 64 px), `show_original` (0 or 1), `opacity` (0 to 100), `show_speaker` (0 or 1) and `theme` (`light`, `dark` or `auto`). Example: `http://127.0.0.1:47821/<token>/?theme=dark&max_lines=1`. Reload the source after you change a setting.

`max_lines` counts rows on screen, as on the native overlay. Older lines leave first; a sentence longer than the limit rolls, so its newest words stay visible and its first words leave. The original stays on one row: its end when the translation rolled, otherwise its start.

The page draws with the same token table as the native overlay (`hermeneus_engine/overlay_tokens.py`). Once the Desktop pane has sent the active Desktop theme, the page uses it in Desktop's own light or dark mode, and an open page changes within about two seconds when the theme changes. Before that it uses the default Nous theme and follows the system appearance. `theme=light` or `theme=dark` forces a mode.

The token is new for every session and every `overlay-page on`, so OBS needs the new address each time. A port that another program uses is an error with the fix in the message; Hermeneus does not pick another port.

### Slash command

In a Hermes session:

```text
/translate start --to tr
/translate start --to de --source "Google Chrome"
/translate start --to tr --speak --device "External Headphones" --voice <voice id>
/translate status
/translate set --to de
/translate set --mode both --device "External Headphones"
/translate voices
/translate stop
/translate overlay show
/translate overlay hide
/translate overlay status
```

`/translate` on its own shows the status. `start` takes the same session flags as `hermes hermeneus run` (`--to`, `--source`, `--speak`, `--device`, `--voice`), and `set` takes the flags of `hermes hermeneus set`. The session runs inside that Hermes process and stops when the process exits or the plugin is unloaded. A missing choice ends the command with a message that names the flag to pass; nothing waits for input.

From a chat platform such as Telegram or Slack, `/translate start` and `/translate set --source` are refused with "Hermeneus captures audio on this Mac, so start it from the Mac (terminal or Hermes Desktop)." (on Windows: "this PC" and "the PC") A start from chat would turn on capture on the Mac that runs Hermes and send that audio to ElevenLabs while nobody may be at the Mac. To allow it, turn on the `allow_remote_start` setting. `/translate status`, `/translate stop` and the other `set` changes work from anywhere. The overlay is refused from a chat platform either way.

### Desktop pane

Hermes Desktop shows a Hermeneus pane at the bottom of the workspace and a **Hermeneus** item on the right of the status bar. Both appear once the plugin is on in Capabilities > Plugins (see First run). The pane has two tabs.

- **Subtitles**: the controls row holds the **Source**, the **Subtitle language** and the **Mode** (Subtitles only, Subtitles and voice, Voice only). With a voice mode, the **Voice and output device** button opens the **Voice** picker (the voices on your ElevenLabs account, read through the same `GET /v2/voices` call as `hermes hermeneus voices`) and the **Output device** picker. The **Subtitle overlay** button opens or closes the overlay while a session runs. **Start** and **Stop** are at the end of the row. Each choice is saved; while a session runs, a changed choice also changes the session at once, as `hermes hermeneus set` does.
- Each line shows the translation, with the original under it in smaller, dimmer text and tagged with the language Scribe heard. A line Scribe is still hearing updates in place and is replaced by its translation when it is ready. Speakers are named Speaker 1, Speaker 2 and so on, each in its own colour. The list follows the newest line; scroll up to read back and it stays where you are, with **Jump to live** and the number of new lines to return. Buttons change the subtitle size.
- **Doctor**: the same checks as `hermes hermeneus doctor`, with **Run checks again**. It runs the capture check with its test tone each time (see [First run](#first-run)); the pane has no way to skip it.

Closing the pane with its close button turns the whole plugin off, status bar item included. That is how Desktop treats a plugin with one pane. Turn it on again in Capabilities > Plugins; it comes back in the same place.

The pane talks to the Hermes gateway through the plugin's backend routes under `/api/plugins/hermeneus/` on Hermes's own server. If the pane says "Hermeneus backend unreachable", enable the plugin under `plugins.enabled` in `config.yaml` and restart the gateway.

> Screenshot placeholder (W16): docs/screenshots/pane-subtitles.png, the Subtitles tab during a session.
>
> Screenshot placeholder (W16): docs/screenshots/pane-doctor.png, the Doctor tab.

### Subtitle overlay

The overlay is a strip near the bottom of the screen that floats above every other app, so the subtitles stay in view while you watch a video in Chrome or another app, including a full-screen video. It shows the current translation in large text and, above it, the sentence that was heard in small text. A line still being spoken appears at once in a lighter colour and its translation takes its place when it arrives. Each speaker turn starts a row of its own. Text that does not fit in the line limit rolls upwards, and older turns leave first. The strip grows and shrinks with its rows, and changes cross-fade in 150 ms unless macOS is set to reduce motion.

- Open it with `hermes hermeneus overlay show`, `/translate overlay show`, the overlay button in the pane's controls row or the one next to the status bar item. While no session runs, those two buttons arm the overlay instead: they turn on the `overlay` setting and it opens with the next session.
- It closes by itself when the session stops, and with `overlay hide`, the same buttons, or **Hide overlay** in its menu.
- It never takes keyboard focus and clicks pass through it to the app underneath. Only two small handles take the mouse: drag the grip at the left edge to move the strip, drag the handle at the right edge to change its width, right-click the grip for width presets, **Reset position** and **Hide overlay**. Position and width are remembered for each display.
- It draws with the Hermes Desktop theme you use. The pane reads the theme's colours, radius and font on its own element and sends them to the backend, and both the overlay and the 127.0.0.1 page use them, in Desktop's light or dark. Until Desktop has sent a theme, the overlay uses the default Nous values and the system appearance.
- Text keeps a soft halo in the background colour, and below 50% background opacity an outline as well, so it stays readable on bright or dark video with no background at all.
- The look popover next to the pane's overlay button sets text size, lines, background opacity, the original line and speaker names. These are the `overlay_*` settings below; a change there or in Settings reaches an open overlay without closing it.
- The overlay opens only on the Mac where the request comes from: a slash command from a chat platform such as Telegram, or a Desktop connected to a remote backend, is refused with a message.

## Configuration

Plugin settings live under `plugins.entries.hermeneus.settings` in your Hermes `config.yaml`. Desktop shows them as a form under **Capabilities > Plugins**. From a terminal:

```bash
hermes config set plugins.entries.hermeneus.settings.target_language tr
```

Command flags override the settings for one session.

| Key | Label | Type | Default | Choices | Description |
| --- | --- | --- | --- | --- | --- |
| `target_language` | Translate into | str | `""` | | Language code to translate into, for example tr or de. The --to flag overrides it. |
| `capture_backend` | Capture | str | `auto` | auto, process-tap, process-loopback, virtual-device | auto uses a Core Audio process tap on macOS 14.2 and later (process-loopback, WASAPI process loopback, on Windows build 20348 and later, preview), otherwise a virtual device such as BlackHole or VB-CABLE. |
| `source` | Audio source | str | `""` | | Audio source to capture, as hermes hermeneus devices lists it. Empty means all system audio with a process tap. The --source flag overrides it. App values differ per system: `app:<bundle id>` on macOS, `app:<exe>` on Windows (preview); `system` works on both. |
| `speak` | Spoken translation | bool | `false` | | Speak the translation as well as showing subtitles. The --speak flag turns it on for one session. |
| `commit_strategy` | Segmenting | str | `vad` | vad, turn_prediction | How Scribe decides a segment is finished. |
| `output_device` | Spoken translation output | str | `""` | | Output device for spoken translation. It must not feed the audio source. |
| `voice_id` | Voice | str | `""` | | ElevenLabs voice id for spoken translation. Empty uses the default voice Talia. |
| `context_segments` | Context lines | int | `3` | | Earlier lines sent with each translation request (0 to 10). |
| `overlay` | Subtitle overlay | bool | `false` | | Open the floating subtitle overlay with every session. It floats above other apps on this Mac and never takes keyboard focus. |
| `overlay_text_size` | Overlay text size | int | `26` | | Translation text size in the overlay, in points (14 to 64). |
| `overlay_max_lines` | Overlay lines | int | `2` | | Lines of translation the overlay shows before older text rolls off (1 to 3). |
| `overlay_show_original` | Overlay shows the original | bool | `true` | | Show the heard sentence in small text under its translation. |
| `overlay_opacity` | Overlay background opacity | int | `80` | | Background opacity of the overlay in percent (0 to 100). The text stays fully opaque. |
| `overlay_show_speaker` | Overlay speaker names | bool | `true` | | Show Speaker 1, Speaker 2 and their colours in the overlay. |
| `allow_remote_start` | Allow start from chat platforms | bool | `false` | | Lets /translate start and /translate set --source work from Telegram, Slack and other chat platforms. Off by default: a start from chat turns on audio capture on this computer and sends that audio to ElevenLabs while nobody may be at the computer. Status and stop work from anywhere either way. |
| `overlay_page` | Overlay page | bool | `false` | | Serve live subtitles on a 127.0.0.1 page for OBS browser sources and second screens while a session runs. hermes hermeneus overlay-page prints its address. |
| `overlay_page_port` | Overlay page port | int | `47821` | | 127.0.0.1 port for the overlay page (1024 to 65535). A port in use is an error; no other port is picked. |

The default voice for spoken translation is Talia (`OZ0L6eISlOejga3XjDFt`), the replacement ElevenLabs publishes for its retiring default voice Sarah (https://elevenlabs.io/docs/help-center/product/voices/my-voices/what-are-default-voices). Talia is a Voice Library voice, and ElevenLabs does not offer Voice Library voices through the API on the free tier, so free accounts set `voice_id` to a voice from their own account. When the chosen voice is not available, Hermeneus falls back to Talia and says so in the status line.

### Translation model

Translation uses the auxiliary task `live_translate`, shown as **Live translation** in the model pickers. Without a pin it uses your main model, which can add seconds per line when that model is large. Pin a fast model with `hermes model` (**Configure auxiliary models > Live translation**), in Desktop under **Settings > Models > Auxiliary**, or in `config.yaml`:

```yaml
auxiliary:
  live_translate:
    provider: openrouter
    model: vendor/model-id
```

The [plugin LLM access guide](https://hermes-agent.nousresearch.com/docs/developer-guide/plugin-llm-access) describes task-routed auxiliary calls.

Providers tested with this release:

| Provider | Model | Result |
|---|---|---|
| `anthropic` | `claude-haiku-5-5` | Translated live English news into Turkish on Hermes v0.23.1. Needs Hermes' optional `anthropic` feature: `hermes pm install --extra anthropic`. |
| `nous` | `anthropic/claude-haiku-5.5` | The call reached Nous Portal and returned `insufficient_credits_for_paid_model` on an account without credits. Translation with credits was not tested. |

Other providers go through the same Hermes route and are untested.

### Secret

`ELEVENLABS_API_KEY` is declared under `requires_env` and read only through Hermes's secret scope (`agent.secret_scope.get_secret`), which follows the active profile. Set it with `hermes config set ELEVENLABS_API_KEY <key>`.

## Languages

Listening covers 100 languages and speaking covers 32.

- Listening: the 100 languages ElevenLabs lists for Scribe v3 at https://elevenlabs.io/docs/capabilities/speech-to-text. Subtitles can be shown in any of these 100 languages. Translation quality for a given language depends on your model.
- Speaking: the 32 languages of `eleven_flash_v2_5` listed at https://elevenlabs.io/docs/overview/models: Arabic, Bulgarian, Croatian, Czech, Danish, Dutch, English, Filipino, Finnish, French, German, Greek, Hindi, Hungarian, Indonesian, Italian, Japanese, Korean, Malay, Mandarin Chinese, Norwegian, Polish, Portuguese, Romanian, Russian, Slovak, Spanish, Swedish, Tamil, Turkish, Ukrainian, Vietnamese.

The full tables, with their sources, are in `hermeneus_engine/languages.py`. Starting with `--speak` and a target outside the speaking list ends with a message that names the problem.

## Latency

Measured end to end on this Mac with real ElevenLabs Scribe v3 Live and Claude Haiku 5.5 (provider `anthropic`) as the translation model. Times run from the end of a spoken sentence, taken from the audio itself, to the event named in each row.

| Stage | Median | p95 | Audio |
| --- | --- | --- | --- |
| Sentence released for translation | 1.01 s | 1.37 s | both, from the plugin's own `latency_ms` |
| Translated subtitle | 2.33 s | 2.64 s | speech with pauses between sentences (32 sentences) |
| Translated subtitle | 2.12 s | 2.68 s | continuous news-style speech (21 sentences) |
| First spoken audio after the request (with `--speak`) | 1.07 s | 1.89 s | 2 utterances only, a weak sample |

Test setup: English synthetic speech played into a virtual input and captured from it, translated into Turkish; an independent auditor's run on 2026-11-11 (`hermes hermeneus status --json` reports the same figures live under `metrics.latency_ms`, within about 0.15 s of the audio-based measurement). Real streams, other voices and other models will differ.

## Cost

Hermeneus uses your own accounts. It has no account of its own and charges nothing.

- Scribe v3 Live: $0.22 per hour of audio on the ElevenLabs API price list (https://elevenlabs.io/pricing/api). ElevenLabs bills all audio you stream, including silence (https://elevenlabs.io/docs/eleven-api/guides/how-to/speech-to-text/realtime/transcripts-and-commit-strategies). Hermeneus stops sending audio after about 10 seconds of digital silence from the source and starts again with the first sound, so a paused video costs little; quiet but not silent audio is still sent. Stop the session when you are done.
- Spoken translation: the same price list gives $0.04 per 1,000 characters for Flash / Turbo text to speech. You pay only while `--speak` is on, for the characters of the translated lines.
- Translation: one request to your Hermes model per committed segment, billed by your model provider at its rates.

Prices change; the ElevenLabs page is the reference.

## Privacy and disclosures

Network:

- **Audio sent to ElevenLabs.** While a session runs, the captured audio is streamed to ElevenLabs Scribe v3 Live at `wss://api.elevenlabs.io/v1/speech-to-text/realtime` with your `ELEVENLABS_API_KEY`. ElevenLabs' own terms cover what they keep.
- **Text sent to ElevenLabs.** With spoken translation on, the translated text is sent to ElevenLabs text to speech at `wss://api.elevenlabs.io/v1/text-to-speech/<voice id>/multi-stream-input`.
- **Voice list.** `hermes hermeneus voices`, `/translate voices` and the pane's voice picker read the voices on your account with `GET https://api.elevenlabs.io/v2/voices`, the key in the `xi-api-key` header. The answer is kept in memory for 60 seconds. The pane asks only when a mode with voice is chosen.
- **Text sent to your model.** Each committed segment is sent to the model Hermes routes the `live_translate` task to, through `ctx.llm`. The request also carries up to 3 earlier lines (`context_segments`, 0 to 10), each with its speaker label and the translation already shown. No audio goes to the model. `hermes hermeneus doctor --test-model` sends one tiny extra request.
- **Starts from a chat.** `/translate start` and `/translate set --source` from a chat platform are refused unless you turn on `allow_remote_start` (off by default). With it on, a chat message can start capture on the Mac that runs Hermes and streaming to ElevenLabs, billed to your account, with no action at that Mac. The refusal covers slash commands; **Start** in a Desktop pane connected to a remote Hermes backend is not refused.
- **Overlay page (off by default).** Only after `hermes hermeneus overlay-page on` (setting `overlay_page`), the process that runs the session listens on TCP port 47821 (`overlay_page_port`) on 127.0.0.1 only, never on another interface, while the session runs. It serves one page and a Server-Sent Events stream of the subtitle lines (original, translation, speaker number, session state). Every address needs a random token that is new each session; requests without it get 403, and requests whose Host header is not `127.0.0.1` or `localhost` with that port get 403, which blocks DNS rebinding. It answers GET and HEAD only, sends no CORS headers, has no control routes and loads nothing from the internet. It stops when the session ends, when the plugin is unloaded and with its process. Any program running as you on this Mac that learns the address can read the subtitles. With the page off, Hermeneus listens on no TCP port on macOS.
- **No telemetry.** Hermeneus sends no usage data anywhere.

Audio and permissions:

- **Which audio is captured.** With the process tap you choose one source: one app with its helper processes (for example Google Chrome), or all system audio except Hermeneus itself. Microphones are not captured. With the fallback, Hermeneus reads the virtual device, which carries whatever you route to it.
- **Permission.** The process tap needs the macOS System Audio Recording permission for the app that runs Hermes (System Settings > Privacy & Security > Screen & System Audio Recording). The virtual device fallback is an input device, so macOS asks for the Microphone permission for that app instead.
- **Doctor test tone.** `hermes hermeneus doctor` and the pane's Doctor tab play a 440 Hz test tone for about half a second to prove capture: on BlackHole or VB-Cable when one is installed, otherwise on your default output at -53.5 dBFS while the tap mutes it (fully silent is not measured). `--no-audio-check` skips it. Details are in [First run](#first-run).

Processes and threads:

- **Session thread.** One session runs at a time, inside the Hermes process that started it, on its own thread. Unloading the plugin stops it.
- **Background capture worker.** The process tap runs in a separate Python worker process (`python -m hermeneus_engine.capture._tap_worker`, the interpreter Hermes runs on), started with the session. It stops when the session stops: Hermeneus closes the worker's input, waits up to 5 seconds, then sends SIGTERM and finally SIGKILL. The worker also stops when its input closes because Hermes exited, on SIGTERM, SIGINT or SIGHUP, and at interpreter exit. It gets a minimal environment (PATH, HOME, TMPDIR, LANG, LC_ALL, LC_CTYPE, its own PYTHONPATH and PYTHONUNBUFFERED), so no API key, token or other secret of the Hermes process reaches it. The fallback runs no extra process.
- **Doctor test player.** The capture check starts one more short-lived Python process (`python -c <player script>`) with the same minimal environment. It plays silence, then the test tone, and is killed after about half a second, before the tap stops.
- **Overlay window process.** The subtitle overlay is a separate Python process (`python -m hermeneus_engine.overlay`, started with the interpreter Hermes runs on and Hermes's import path), because macOS windows belong to a process's main thread. It runs only while you have it open, draws two borderless windows above other apps on every Space and next to full-screen windows, and reads subtitles from the local session socket. It ends when the session stops, on `overlay hide` or SIGTERM. It needs no macOS permission, opens no network connection and no TCP port, and does not read the keyboard; it only takes mouse clicks on its own grip. It refuses to start without a desktop session (over SSH or in a background service). It gets the same minimal environment as the capture worker plus Hermes's import path, so no API key, token or other secret of the Hermes process reaches it.
- **Shell commands.** Every helper process starts through one module, `hermeneus_engine/subproc.py`, by absolute path, as an argument list (no shell), with the minimal environment. Hermeneus runs `/usr/sbin/system_profiler SPAudioDataType -json` (read only) to read audio device names and to check that spoken output cannot be captured again. To name the app in the permission message, it walks the parent processes of Hermes with `/bin/ps -o ppid=,comm= -p <pid>` (read only, at most 32 steps).

Local state:

- **Local socket.** Other Hermes processes reach the session through a Unix domain socket at `<HERMES_HOME>/plugin-data/hermeneus/run/sock` (by default `~/.hermes/plugin-data/hermeneus/run/sock`), mode 0600 inside a 0700 directory. The Desktop pane uses routes mounted on Hermes's own server.
- **Overlay page (off by default).** Only after `hermes hermeneus overlay-page on` (setting `overlay_page`), the process that runs the session listens on TCP port 47821 (`overlay_page_port`) on 127.0.0.1 only, never on another interface, while the session runs. It serves one page and a Server-Sent Events stream of the subtitle lines (original, translation, speaker number, session state). Every address needs a random token that is new each session; requests without it get 403, and requests whose Host header is not `127.0.0.1` or `localhost` with that port get 403, which blocks DNS rebinding. It answers GET and HEAD only, sends no CORS headers, has no control routes and loads nothing from the internet. It stops when the session ends, when the plugin is unloaded and with its process. Any program running as you on this Mac that learns the address can read the subtitles.
- **Files written.** A lock file at `run/lock` next to the socket holds the owning process id, its kind (CLI, Hermes or gateway), the start time and the socket path, and is emptied when the session stops. The subtitle overlay keeps five small files in `<HERMES_HOME>/plugin-data/hermeneus/overlay/` (a 0700 directory): `lock` (its process id while it runs), `settings.json` (the overlay settings it was given), `theme.json` (the Desktop theme colours, radius and font the pane sent), `positions.json` (its place and width per display) and `overlay.log` (its error output, replaced at each start). No subtitle text is written. Nothing else is written.
- **No audio or transcript stored.** Subtitles stay in memory, the last 200 lines, and are gone when the session ends. No audio and no transcript is written to disk.

Software and credentials:

- **Third-party software.** BlackHole 2ch or VB-Cable is needed only for the fallback. You install it yourself from its official source; Hermeneus does not bundle, download or install any driver.
- **Credentials.** The ElevenLabs key is read only through Hermes's secret scope and never stored or printed by Hermeneus.

## Troubleshooting

Start with `hermes hermeneus doctor`. Its checks, and what to do when one is not `ok`:

| Check | When it fails | Fix |
| --- | --- | --- |
| `macOS` | Older than 13, or older than 14.2 (warning: only the fallback works) | Update macOS to 14.2 or later, or set up the fallback below. |
| `capture: process-tap` | Process taps are not available on this Mac | Process taps need macOS 14.2 or later; use the fallback on older macOS. |
| `capture: virtual-device` | No BlackHole or VB-Cable input found | Needed only for the fallback. Install BlackHole 2ch (`brew install blackhole-2ch`, source https://github.com/ExistentialAudio/BlackHole) and run the doctor again. |
| `permission: system audio recording` | The test tone arrived as silence, or the check was skipped with `--no-audio-check` (warning) | Allow the app that runs Hermes under System Settings > Privacy & Security > Screen & System Audio Recording (System Audio Recording Only is enough), quit and reopen it, and run the doctor again. |
| `audio source` | No capture backend can run | See the capture lines above it. |
| `permission: microphone (virtual device)` | Fallback only; the first start asks macOS | Allow the app that runs Hermes under System Settings > Privacy & Security > Microphone, then start again. |
| `system sound reaches the virtual device` | Fallback only; system output does not include the virtual device | Create a Multi-Output Device and choose it as the system output (see the next section). |
| `ELEVENLABS_API_KEY` | Not set in this profile | Create a key at https://elevenlabs.io/app/settings/api-keys and run `hermes config set ELEVENLABS_API_KEY <key>`. |
| `translation model` | Warning: `auxiliary.live_translate` has no model | Pick a fast model with `hermes model` (Configure auxiliary models > Live translation). |
| `ffmpeg` | Never fails | Hermeneus does not need ffmpeg. |
| `translation model feature` | The `live_translate` provider needs an optional Hermes feature that is not installed | Run the command in the fix line, for example `hermes pm install --extra anthropic`. |
| `translation model (live call)` | Only with `--test-model`: the one test request failed | Pick another model with `hermes model` (Configure auxiliary models > Live translation). |
| `overlay page` | The page is on but could not start, for example because the port is in use | Free the port or pick another with `hermes hermeneus overlay-page on --port <port>`. |

Every failure message a session can show, with its exact text and where it appears, is listed in [docs/failures.md](docs/failures.md).

Other problems:

- **The session starts but no subtitles appear while audio plays.** With the process tap, Hermeneus notices when the tap delivers only silence while the app reports that it is playing. It rebuilds the tap up to five times and then shows a message. If the message says macOS is giving silence, allow the app that runs Hermes under System Settings > Privacy & Security > Screen & System Audio Recording and start again. Check that the source is the app that is playing (`hermes hermeneus devices` marks apps that are playing now, on any output device; it lists apps with a Dock icon, so a command-line player does not appear).
- **The captured app quit, or you changed the output device.** No action is needed with the process tap. When the app starts again, capture follows its new processes within about a second. When the default output or the device list changes, capture is rebuilt. The status line says so in both cases.
- **"Capture did not start within 30 s".** macOS was probably showing the permission prompt. Allow it and start again.
- **"no audio source chosen".** More than one virtual device is installed and no source was given. Pass `--source <name>` or set `source`.
- **"--source matches more than one source".** Pass the full name as `hermes hermeneus devices` prints it.
- **Spoken translation is refused with a feedback warning.** The output device you chose would feed the audio back into the capture source, for example a Multi-Output Device that contains BlackHole while you capture BlackHole. Pick your speakers or headphones directly. With the process tap this cannot happen, because Hermeneus excludes its own process from the capture.
- **"a session is already running".** Only one session runs at a time. `hermes hermeneus status` shows it and `hermes hermeneus stop` ends it.
- **Subtitles lag by several seconds.** Pin a small, fast model to `live_translate`.
- **The Desktop pane says "Hermeneus backend unreachable".** Enable the plugin (`hermes plugins enable hermeneus`) and restart the gateway.

## Fallback: virtual device with a Multi-Output Device

Use this on macOS 13 to 14.1, or when you prefer a virtual device. The process tap does not need any of it.

1. Install a virtual loopback device. BlackHole 2ch: `brew install blackhole-2ch`, or the installer from https://github.com/ExistentialAudio/BlackHole. VB-Cable: the installer from https://vb-audio.com/Cable/.
2. Open **Audio MIDI Setup** (in **Applications > Utilities**).

   > Screenshot placeholder (W16): docs/screenshots/fallback-1-audio-midi-setup.png, the Audio MIDI Setup window.

3. Click the **+** button at the bottom left and choose **Create Multi-Output Device**.

   > Screenshot placeholder (W16): docs/screenshots/fallback-2-create-multi-output.png, the + menu.

4. In the new device, tick your speakers or headphones and **BlackHole 2ch** (or **VB-Cable**). Choose your speakers or headphones as the **Primary Device** and turn on **Drift Correction** for the virtual device.

   > Screenshot placeholder (W16): docs/screenshots/fallback-3-members.png, the Multi-Output Device with both devices ticked.

5. Optionally rename the device, for example to "Speakers + BlackHole", by double-clicking its name in the list.
6. Open **System Settings > Sound > Output** and choose the Multi-Output Device. You keep hearing your audio and the virtual device receives a copy.

   > Screenshot placeholder (W16): docs/screenshots/fallback-4-sound-output.png, System Settings > Sound with the Multi-Output Device selected.

7. Tell Hermeneus to use it: `hermes config set plugins.entries.hermeneus.settings.capture_backend virtual-device`, or pass `--source "BlackHole 2ch"` for one session.
8. Run `hermes hermeneus doctor`. The line `system sound reaches the virtual device` should read `ok`.
9. Start a session. macOS asks for the Microphone permission for the app that runs Hermes; click **Allow**.

For spoken translation on the fallback, choose your speakers or headphones as the output device, never the Multi-Output Device that contains the virtual device. Hermeneus refuses an output that would feed back into the capture.

To undo the setup, choose your speakers again in System Settings > Sound and delete the Multi-Output Device in Audio MIDI Setup.

## Windows (preview)

Status: implemented and reviewed against documentation, not yet run on a Windows machine. macOS is tested on real audio; Windows is not. The catalog entry lists macOS only, so `hermes plugins install hermeneus` refuses to install on Windows. To try the preview, install from the repository URL; Hermes v0.23.1 does not gate loading on the `platforms` field of `plugin.yaml`.

### Requirements on Windows

- **Driverless capture needs OS build 20348 or later.** Microsoft gives "Windows 10 Build 20348" as the minimum client for the process loopback parameters ([AUDIOCLIENT_ACTIVATION_PARAMS](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_activation_params), [AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params)), and Microsoft's sample says the same. Build 20348 is Windows Server 2022 ([Windows Server release information](https://learn.microsoft.com/en-us/windows/release-health/windows-server-release-info)); no Windows 10 client release has that build. The first client release at or above it is Windows 11 21H2, build 22000 ([Windows 11 release information](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)). So on a desktop PC the requirement is Windows 11. The [ActivateAudioInterfaceAsync](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync) page says "Build 20438" instead, probably two swapped digits; Hermeneus follows the two pages and the sample that say 20348.
- **Older Windows** uses the virtual device fallback with VB-CABLE, which you install yourself.
- **Python packages:** `comtypes`, `pycaw` and `psutil`, installed from PyPI wheels on Windows only. Nothing compiled is in the repository.

### What is the same as on macOS

- Scribe v3 Live, sentence-level translation through your Hermes model, subtitles in the Desktop pane and the terminal, the 127.0.0.1 overlay page, and spoken translation with Flash v2.5.
- The commands `/translate start|set|stop|status|voices` and `hermes hermeneus run|set|stop|status|voices|devices|doctor|overlay-page`.
- Every config key and its default. `capture_backend` gains one value, `process-loopback`, and `auto` picks it on Windows.
- `--source system` (or an empty `source`) captures all system audio except the Hermeneus process tree. Spoken translation plays from that tree, so it is never captured, on any output device.
- When capture delivers only silence or stops (no-audio or stalled), the session ends with a message, as on macOS. The texts are listed in [docs/failures.md](docs/failures.md).

### What differs from macOS

| Area | macOS | Windows |
|---|---|---|
| Floating subtitle popup (`overlay`, `/translate overlay`, the pane's overlay switch) | NSPanel window | Not available. The command answers that the overlay is built for macOS; the six `overlay_*` settings have no effect. Use the Desktop pane or `hermes hermeneus overlay-page`. |
| Source names | `app:<bundle id>`, for example `app:com.google.Chrome` | `app:<exe>`, for example `app:chrome.exe`. A `source` setting written on one system does not match on the other; `system` matches on both. |
| App list | apps with audio, from Core Audio | apps with an audio session on the default output device. An app that plays only to another device is reachable with `--source system`. |
| `pid:<pid>` source | that process | that process and its children. A pid of Hermeneus or one of its parents (for example Hermes Desktop) is refused, because its tree would include Hermeneus's own speech. |
| No-audio message | names System Audio Recording | names Settings > Privacy & security > Microphone > Let desktop apps access your microphone. Whether that switch silences process loopback is an assumption (R11 below). |
| Doctor | macOS version, real-audio capture check, System Audio Recording and Microphone lines, Multi-Output check | Windows build, no real-audio check (`--no-audio-check` is accepted and does nothing), a Microphone privacy line it cannot read, a VB-CABLE route check for the fallback, the default output device |
| Feedback guard on the fallback | BlackHole has one name, so the same-device rule applies | VB-CABLE's two names, CABLE Input and CABLE Output, count as one device |
| Session transport | Unix socket, mode 0600, no TCP port | TCP on 127.0.0.1 with a port the system picks and a 256-bit token in `<HERMES_HOME>\plugin-data\hermeneus\run\sock`. The 0600 mode does nothing on Windows ("Windows only supports setting the file's read-only flag", [os.chmod](https://docs.python.org/3/library/os.html#os.chmod)); privacy rests on the per-user ACL of `%LOCALAPPDATA%\hermes` (R16). |
| Capture worker stop | stdin closed, then SIGTERM, then SIGKILL | stdin closed, then TerminateProcess after 5 s |
| Ctrl+C in `hermes hermeneus run` | loop signal handlers | `signal.signal` for SIGINT and SIGBREAK |
| Wording | "this Mac" | "this PC" |

### Adding a Windows popup later

The overlay's logic is platform neutral (`hermeneus_engine/overlay/app.py`, `model.py`, `placement.py`). A window backend implements the `Panel` protocol in `overlay/app.py`: `displays()`, `main_display()`, `measure(text, run, settings)`, `measure_original(text, settings)`, `strip_height(settings)`, `text_inset()`, `set_frame(frame)`, `render(view, settings)` and `set_visible(visible)`. A Windows panel would need a topmost, borderless, click-through-optional layered window (`WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`) that does not take focus, per-monitor DPI awareness, text measurement with DirectWrite or GDI, a message loop in the overlay subprocess, and `overlay/control.py` changes for Windows (it uses `fcntl`, `select` on a pipe and SIGTERM today). It is not built: it could not be tested without a Windows machine, and whether to add it is the owner's decision.

### Assumptions only a Windows run can confirm

From the A4 review (COM and WASAPI), R1 to R19:

- R1 ActivateAudioInterfaceAsync accepts the comtypes handler (it answers IAgileObject and now also aggregates the free-threaded marshaler) and ActivateCompleted runs.
- R2 The ctypes PROPVARIANT and parameter blob are accepted and activation returns a real IAudioClient.
- R3 Initialize in shared, event-driven mode with buffer duration 0 and periodicity 0 is accepted (now as Microsoft's Initialize page requires).
- R4 48 kHz float32 stereo with AUTOCONVERTPCM is accepted; if not, the sample's 44.1 kHz 16-bit PCM is, on a fresh client.
- R5 (dropped) SRC_DEFAULT_QUALITY is no longer passed; the flags are the sample's.
- R6 The event fires about once per engine period, packets arrive, and pu64QPCPosition is filled and plausible.
- R7 EXCLUDE on the Hermes process removes Hermeneus's spoken translation from the system capture (`scripts/windows_smoke.py --tone`).
- R8 INCLUDE and EXCLUDE trees cover child processes started after activation.
- R9 Process loopback captures streams on every endpoint, not only the default one.
- R10 No rebuild is needed after a default-device change or a device rate change.
- R11 No consent prompt appears on activation (unpackaged interpreter and MSIX Hermes Desktop), and whether the Microphone privacy switch silences process loopback.
- R12 `sys.coinit_flags = 0` before the first comtypes import puts the worker in the MTA; CoInitializeEx on the pump and tick threads succeeds; pycaw session listing works in the MTA.
- R13 How long one periodic tick takes (now on its own thread and recorded as `tick_ns_max`), and capture-to-PCM latency against the macOS tap.
- R14 TerminateProcess on the worker also ends the real interpreter when `sys.executable` is a venv launcher, and the audio service releases the client.
- R15 The worker starts with the minimal environment and the parent's search path from the Hermes interpreter (uv venv and MSIX).
- R16 `%LOCALAPPDATA%\hermes` ACLs keep the endpoint file and its token private to the user.
- R17 `msvcrt.locking` raises EACCES when another handle holds the byte, and truncating at offset 0 works while the byte at 1 GiB is locked.
- R18 Ctrl+C, Ctrl+Break and closing the console window stop `hermes hermeneus run` cleanly.
- R19 Hermes `ctx.llm`, `get_secret`, the Desktop pane and the TCP session client behave as on macOS.
- R20 QueryInterface(IID_IMarshal) on the completion handler returns the free-threaded marshaler's IMarshal (custom routing in `_wasapi.marshal_query`), and activation still completes.
- R21 Repeated rebuilds (silence, app restart, stream errors) never hang or crash the pump. Only the pump closes a stream it may be waiting on, after its wait returns; WaitForSingleObject's behaviour is undefined when its handle closes during the wait.
- R22 Worst-case start time when the first format is refused, which costs a second activation.
- R23 Spoken translation opens the WASAPI entry of the output device at 24 kHz with `WasapiSettings(auto_convert=True)` and plays at the right pitch and speed, whatever the mixer rate is (sounddevice: auto_convert "is in particular required for streams not matching the system mixer sample rate").
- R24 A tick thread still inside an activation when capture stops keeps running until that activation returns or the worker exits; the stream it gets is closed, never started, and the worker still exits within the parent's 5 s stop window.

From the A5 review (parity and packaging):

- Hermes on Windows puts plugin dependencies on `sys.path` the way it does on macOS, so the worker finds them through the parent's search path.
- PortAudio lists each endpoint once per host API under the same name, and the WASAPI entry plays (the device list shape is modelled, not read from a Windows machine).
- The VB-CABLE names are "CABLE Input (VB-Audio Virtual Cable)" and "CABLE Output (VB-Audio Virtual Cable)" (numbered cables "CABLE-A Input" and so on).
- A hosted Windows runner has no audio endpoint (so CI proves installs, imports, the lock, the transport and the doctor, not capture).
- An app that quits keeps the stream open and silent until it starts again, and the capture follows the new process.

The preview label stays until these are checked.

## Limits and known issues

- Tested on macOS. Windows is a preview that has not yet run on a Windows machine (see above). Linux support is planned.
- One session at a time. A second start reports the running session.
- The overlay is macOS only. A Windows equivalent (a layered, topmost, non-activating window behind the same interface in `hermeneus_engine/overlay/app.py`) is designed but not built.
- The overlay was measured above a native full-screen window in its own Space on one built-in display. Several displays and moving between ordinary Spaces were not measured.
- The doctor learns the System Audio Recording permission only from its test capture. With `--no-audio-check` it shows the permission as a warning.
- The pane's Doctor tab always runs the capture check with its test tone.
- In voice only mode the displays still show the subtitle lines.
- The process tap reads audio before the system output volume is applied, so the volume setting does not change what Hermeneus hears.
- Spoken translation covers 32 languages and needs an output device.
- Speaker labels come from Scribe's diarization. Scribe can merge two labels during a session; Hermeneus relabels the lines it still shows when that happens.
- Translation runs on your model. Its speed sets how far subtitles trail the speech, and its skill in a language sets their quality.
- Scribe bills streamed audio including silence, so an idle session still costs money.

## License

MIT. See [LICENSE](LICENSE).
