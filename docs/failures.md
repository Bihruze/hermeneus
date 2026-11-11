# Failure messages

Every failure the user can meet, the exact text, where it appears and the
test that triggers it with fakes. Texts live in `hermeneus_engine/messages.py`
unless another file is named. "status" means `status.message` (the pane's
status line, `hermes hermeneus status`, `/translate status`); "line" means a
failed line's `error`; "doctor" means `hermes hermeneus doctor` and
`GET /doctor`. A failure marked "session ends" moves the state to `failed`.

| # | Failure | Text | Where | Test |
|---|---|---|---|---|
| 1a | no ElevenLabs key, speech to text | Speech to text: no ElevenLabs API key is set. Add ELEVENLABS_API_KEY to Hermes (see: hermes hermeneus doctor), then start translation again. | status, session ends; doctor line `ELEVENLABS_API_KEY: not set` | `test_failure_messages.py::test_no_key_speech_to_text` |
| 1b | no ElevenLabs key, spoken translation | Spoken translation: no ElevenLabs API key is set. Add ELEVENLABS_API_KEY to Hermes (subtitles continue). | status | `test_no_key_spoken_translation_keeps_subtitles` |
| 1c | no ElevenLabs key, voice list | Voices: no ElevenLabs API key is set. Add ELEVENLABS_API_KEY to Hermes (see: hermes hermeneus doctor). | `hermes hermeneus voices`, `GET /voices` (502) | `test_voice_catalog.py::test_no_key_and_bad_shape` |
| 2a | invalid key, speech to text | Speech to text: ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in Hermes (a new key is at https://elevenlabs.io/app/settings/api-keys), then start translation again. | status, session ends | `test_invalid_key_speech_to_text` |
| 2b | key without Speech to Text permission | Speech to text: the ElevenLabs API key has no Speech to Text permission. Allow it for the key at https://elevenlabs.io/app/settings/api-keys, then start translation again. | status, session ends | `test_invalid_key_spoken_translation_and_no_stt_permission` |
| 2c | invalid key, spoken translation | Spoken translation: ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in Hermes (subtitles continue). | status | same |
| 2d | invalid key, voice list | Voices: ElevenLabs rejected the API key (HTTP 401). Check ELEVENLABS_API_KEY in Hermes (see: hermes hermeneus doctor). | voices command, `GET /voices` | `test_voice_catalog.py::test_errors_are_plain` |
| 3 | no System Audio Recording permission, zero audio | macOS is giving Hermeneus silence instead of audio. Open System Settings > Privacy & Security > Screen & System Audio Recording, turn on '<app>' under System Audio Recording, then start translation again. (W02 watchdog, `capture/process_tap.py permission_message`) | status, session ends; doctor line `permission: system audio recording` | `test_no_permission_zero_audio_ends_with_the_watchdog_text` |
| 4 | no credits on the model provider | Translation failed: the model provider reports insufficient credits (<provider>). Add credits with that provider, or pick another model with: hermes model (Configure auxiliary models > Live translation) (`glue.describe_llm_error`) | line and status (once per reason, at most every 30 s) | `test_no_credits_is_a_line_error_and_a_status_message` |
| 5a | model needs a missing Hermes extra, at start | Could not start translation: the translation model (<provider>) needs Hermes' '<extra>' feature, which is not installed. Run: hermes pm install --extra <extra> (`glue.model_preflight`) | start refusal; doctor line `translation model feature` | `test_model_missing_extra_at_start_and_in_a_call` |
| 5b | model needs a missing extra, during a call | Translation failed: the translation model needs Hermes' '<extra>' feature. Run: <install hint> | line and status | `test_commands.py::test_llm_errors_become_plain_reasons_with_the_fix` |
| 6 | model slow (lines over the 8 s lag bound or timed out) | Translation: the model is slower than the speech; <n> lines in the last 20 s came late and were skipped or timed out. Pick a faster model with: hermes model (Configure auxiliary models > Live translation). Each line keeps its own error: "Skipped to keep up: the translation would have been more than 8 s behind." or "Translation timed out after 6 s." | status after 2 late lines in 20 s; line | `test_model_slow_after_two_late_lines` |
| 7a | ElevenLabs quota used up, speech to text | Speech to text: the ElevenLabs quota is used up, so transcription stopped. Add credits or change the plan at https://elevenlabs.io/app/subscription, then start translation again. | status, session ends | `test_quota_used_up_speech_to_text` |
| 7b | rate limited, speech to text | Speech to text: ElevenLabs is rate limiting this account; reconnecting. Subtitles resume when it accepts audio again. | status, state `reconnecting` | `test_rate_limited_speech_to_text_reconnects` |
| 7c | quota or rate limit, spoken translation | Spoken translation: the ElevenLabs quota is used up. Add credits or change the plan at https://elevenlabs.io/app/subscription, or turn speech off (subtitles continue). / Spoken translation: ElevenLabs is rate limiting this account; some sentences are not spoken. Speech resumes when it allows more (subtitles continue). | status | `test_quota_and_rate_limit_spoken_translation_from_the_server` |
| 8 | voice unavailable | Voice <id> is not available on this ElevenLabs account; using the default voice Talia. (`tts/voices.py unavailable_message`). When Talia is unavailable too: The default voice Talia (OZ0L6eISlOejga3XjDFt) is not available on this ElevenLabs account. Set voice_id to a voice from your account. | status | `test_voice_unavailable_falls_back_and_says_so`; `test_tts_flash.py` for the fallback itself |
| 9a | network lost, speech to text reconnecting | Speech to text: the connection to ElevenLabs was lost; reconnecting. Check the network connection if this does not clear. | status, state `reconnecting` | `test_network_lost_reconnecting_then_failed` |
| 9b | network lost, reconnects used up | Speech to text stopped: the connection to ElevenLabs was lost and did not come back (<client detail>). Check the network connection, then start translation again. | status, session ends | same |
| 9c | network lost, spoken translation | Spoken translation: the connection to ElevenLabs was lost, so some sentences are not spoken. Check the network connection (subtitles continue). | status | `test_network_lost_spoken_translation` |
| 10 | output device gone | Spoken translation stopped: the output device '<device>' is no longer available. Pick another output device and turn speech on again (subtitles continue). Speech turns off; subtitles keep running. | status | `test_output_device_gone_turns_speech_off_and_subtitles_continue`, `test_playback_health_reports_a_stream_that_stopped_by_itself` |
| 11a | source app quit | Capture from <app> stopped unexpectedly, for example because the app quit. Start translation again, or pick another source (see: hermes hermeneus devices). | status, session ends | `test_source_app_quit` |
| 11b | source stopped delivering audio (W02 watchdog) | Capture from <source> stopped receiving audio. Stop and start translation again. | status, session ends | `test_source_stalled_keeps_the_watchdog_text` |
| 3w | Windows (preview): process loopback delivers only silence while a captured app plays | Windows is giving Hermeneus silence instead of audio. Open Settings > Privacy & security > Microphone, turn on Let desktop apps access your microphone, then start translation again. (`capture/process_loopback.py NO_AUDIO_TEXT`; the cause named is an assumption until a Windows run) | status, session ends; doctor line `permission: microphone (Windows privacy)` | `test_windows_review.py::test_w5_no_audio_ends_the_session_like_macos` |
| 11bw | Windows (preview): source stopped delivering audio | Capture from <source> stopped receiving audio. Stop and start translation again. (same text as 11b) | status, session ends | `test_windows_review.py::test_w5_stalled_uses_the_macos_text_and_ends_the_session` |
| 11cw | Windows (preview): the stream was rebuilt, or the app was restarted and capture moved to the new process | The capture stream failed and was reopened. / Capture delivered silence while audio was playing; the stream was rebuilt. / <exe> was restarted; capture follows the new process. | status (session continues) | `test_windows_review.py::test_l4_rebuilt_and_retargeted_texts_reach_the_status_line` |
| 12w | Windows (preview): a pid source that holds Hermeneus | Process <pid> is Hermeneus or one of its parent processes; capturing it would capture Hermeneus's own speech. Use --source system instead. | start refusal | `test_windows_review.py::test_w14_pid_of_an_ancestor_is_refused` |
| 13w | Windows: capture_backend set to the other system's backend | capture_backend is process-tap, the macOS process tap; this system does not offer it. Set capture_backend to auto (or process-loopback on Windows). (and the reverse on macOS for process-loopback; `options.WRONG_OS`) | start refusal | `test_windows_review.py::test_w11_wrong_os_capture_backend_names_the_platform` |

## Refusals of live changes

`/translate set`, `hermes hermeneus set` and `POST /settings` answer at once.
A refusal changes nothing.

| Refusal | Text |
|---|---|
| no session | No translation session is running. Start one with /translate start or hermes hermeneus run. |
| speech on without a device | Spoken translation needs an output device. Pass --device NAME (see: hermes hermeneus devices). |
| language without a voice while speaking | Spoken translation does not support <language>. Keep speech off for this language (--speak off), or pick a language that spoken translation supports. |
| bad voice id | '<id>' is not an ElevenLabs voice id. List the voices with: hermes hermeneus voices |
| background change failed | Spoken translation not started: <reason>. / Spoken translation not changed: <reason>. / Source not changed: <reason> Still capturing <old source>. (status) |
