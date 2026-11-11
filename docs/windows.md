# Hermeneus on Windows (preview)

This page holds the Windows details. The README's [Windows (preview)](../README.md#windows-preview) section has the summary.

Status: implemented and reviewed against documentation, not yet run on a Windows machine. macOS is tested on real audio; Windows is not. The catalog entry lists macOS only, so `hermes plugins install hermeneus` refuses to install on Windows. To try the preview, install from the repository URL; Hermes v0.23.1 does not gate loading on the `platforms` field of `plugin.yaml`.

## Requirements on Windows

- **Driverless capture needs OS build 20348 or later.** Microsoft gives "Windows 10 Build 20348" as the minimum client for the process loopback parameters ([AUDIOCLIENT_ACTIVATION_PARAMS](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_activation_params), [AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS](https://learn.microsoft.com/en-us/windows/win32/api/audioclientactivationparams/ns-audioclientactivationparams-audioclient_process_loopback_params)), and Microsoft's sample says the same. Build 20348 is Windows Server 2022 ([Windows Server release information](https://learn.microsoft.com/en-us/windows/release-health/windows-server-release-info)); no Windows 10 client release has that build. The first client release at or above it is Windows 11 21H2, build 22000 ([Windows 11 release information](https://learn.microsoft.com/en-us/windows/release-health/windows11-release-information)). So on a desktop PC the requirement is Windows 11. The [ActivateAudioInterfaceAsync](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync) page says "Build 20438" instead, probably two swapped digits; Hermeneus follows the two pages and the sample that say 20348.
- **Older Windows** uses the virtual device fallback with VB-CABLE, which you install yourself.
- **Python packages:** `comtypes`, `pycaw` and `psutil`, installed from PyPI wheels on Windows only. Nothing compiled is in the repository.

## What is the same as on macOS

- Scribe v3 Live, sentence-level translation through your Hermes model, subtitles in the Desktop pane and the terminal, the 127.0.0.1 overlay page, and spoken translation with Flash v2.5.
- The commands `/translate start|set|stop|status|voices` and `hermes hermeneus run|set|stop|status|voices|devices|doctor|overlay-page`.
- Every config key and its default. `capture_backend` gains one value, `process-loopback`, and `auto` picks it on Windows.
- `--source system` (or an empty `source`) captures all system audio except the Hermeneus process tree. Spoken translation plays from that tree, so it is never captured, on any output device.
- When capture delivers only silence or stops (no-audio or stalled), the session ends with a message, as on macOS. The texts are listed in [docs/failures.md](failures.md).

## What differs from macOS

| Area | macOS | Windows |
|---|---|---|
| Floating subtitle popup (`overlay`, `/translate overlay`, the pane's overlay switch) | NSPanel window | Not available. The command answers that the overlay is built for macOS, and the `overlay` setting has no popup to open. The eight look settings (`overlay_text_size`, `overlay_max_lines`, `overlay_show_original`, `overlay_opacity`, `overlay_show_speaker`, `overlay_style`, `overlay_pace` and `overlay_reading_cps`) still set the overlay page. Use the Desktop pane or `hermes hermeneus overlay-page`. |
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

The Windows endpoint file `run/sock` is written to `run/sock.tmp` first and renamed over it at once.

## Adding a Windows popup later

The overlay's logic is platform neutral (`hermeneus_engine/overlay/app.py`, `model.py`, `placement.py`). A window backend implements the `Panel` protocol in `overlay/app.py`: `displays()`, `main_display()`, `measure(text, run, settings)`, `measure_original(text, settings)`, `strip_height(settings)`, `text_inset()`, `set_frame(frame)`, `render(view, settings)` and `set_visible(visible)`, and it calls `OverlayApp.scroll(rows, now)` and `back_to_live(now)` from its history controls. The display queue and the roll-up rows are platform neutral too (`overlay/pace.py`). A Windows panel would need a topmost, borderless, click-through-optional layered window (`WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE`) that does not take focus, per-monitor DPI awareness, text measurement with DirectWrite or GDI, a message loop in the overlay subprocess, and `overlay/control.py` changes for Windows (it uses `fcntl`, `select` on a pipe and SIGTERM today). It is not built: it could not be tested without a Windows machine, and whether to add it is still open.

## Assumptions only a Windows run can confirm

From the review of the COM and WASAPI code, R1 to R24:

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

From the review of parity and packaging:

- Hermes on Windows puts plugin dependencies on `sys.path` the way it does on macOS, so the worker finds them through the parent's search path.
- PortAudio lists each endpoint once per host API under the same name, and the WASAPI entry plays (the device list shape is modelled, not read from a Windows machine).
- The VB-Audio device names follow "{product} Input|Output ({card})", for example "CABLE Input (VB-Audio Virtual Cable)" and "CABLE Output (VB-Audio Virtual Cable)", "CABLE-A Input", "Hi-Fi Cable Input" and "VoiceMeeter Aux Output", and MME cuts them to 31 characters. Only the CABLE form is confirmed in published material; the feedback guard pairs the two sides of each product by that pattern.
- A hosted Windows runner has no audio endpoint (so CI proves installs, imports, the lock, the transport and the doctor, not capture).
- An app that quits keeps the stream open and silent until it starts again, and the capture follows the new process.

The preview label stays until these are checked.
