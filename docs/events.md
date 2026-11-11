# Hermeneus events and backend routes

This is the contract between the session owner, the desktop pane
(`desktop/plugin.js`) and `hermes hermeneus run --json`. The same event
objects travel on the WebSocket `/events`, on the session's Unix socket and in
`GET /lines` pages.

## Sequence numbers

Every event carries `seq`. One counter serves line, relabel and status events,
and within one process it only increases, across sessions too. A consumer
keeps the highest `seq` it has applied and ignores an event whose `seq` is not
higher. After a gap (socket dropped, process restarted) it asks
`GET /lines?after=<seq>`.

## Event types

```json
{"type": "hello", "version": 1, "seq": 41, "session": {"state": "live", "seq": 41, "...": "status fields"}}
{"type": "line", "seq": 42, "line": {"line_id": "...", "state": "partial", "original": "...", "translation": "", "speaker_id": "s1:speaker_0", "source_language": "en", "target_language": "tr", "segment_index": 7, "error": ""}}
{"type": "relabel", "seq": 43, "from": "s1:speaker_1", "to": "s1:speaker_0"}
{"type": "status", "seq": 44, "state": "live", "message": "", "source": {"backend": "virtual-device", "source_id": "BlackHole 2ch", "label": "BlackHole 2ch"}, "target_language": "tr", "detected_language": "en", "speak": false, "mode": "subtitles", "subtitles": true, "voice_id": null, "output_device": null, "applying": [], "metrics": {}, "languages": [{"code": "tr", "name": "Turkish", "speakable": true}]}
{"type": "reset", "seq": 44}
```

- `hello` opens every accepted socket. `session` holds the latest status
  fields and the latest `seq`.
- `line`: the same `line_id` updates a line in place. A line holds one
  sentence: a sentence is sent for translation as soon as the partial
  transcript shows it complete and unchanged (`hermeneus_engine/sentences.py`),
  and the partial line keeps only the words after it. When Scribe commits the
  segment, its remaining sentences become their own lines. A line that ended
  up with no words (Scribe dropped them at commit) is sent as `translated`
  with empty `original` and `translation`; consumers may hide it. `state` is `partial`
  (still changing), `committed` (final original, translation pending),
  `translated`, or `failed` (translation failed or was skipped to keep up; the
  original stays, `error` says why).
- `relabel`: every earlier line with speaker `from` now belongs to `to`.
  Speaker ids look like `s<N>:speaker_<k>`; `N` counts Scribe sessions, so ids
  from different `N` may or may not be the same person.
- `status.state` is one of `idle`, `starting`, `live`, `reconnecting`,
  `stopping`, `failed`. `message` is plain language. `metrics` holds counters
  (`audio_chunks_in`, `audio_chunks_dropped`, `audio_dropped_ms`,
  `audio_queued_ms`, `lines_committed`, `lines_translated`, `lines_failed`,
  `speak_errors`, `sentences_released_early`, `settings_changes`,
  `translation_backlog`, `uptime_s`). Failure messages and where they appear
  are listed in `docs/failures.md`. When translations fail, `message` names the reason and the
  fix once, again when it changes, and at most every 30 s otherwise; it is
  cleared by the next translated line.
- `status.mode` is `subtitles` (subtitles only), `both` (subtitles and
  spoken translation) or `voice` (spoken translation, subtitles hidden).
  In `voice` mode the bus still carries every line, so the overlay, the pane
  and `hermes hermeneus run` keep working; `subtitles` is false and displays
  hide the subtitle text. `speak` is true in `both` and `voice`.
  `voice_id` and `output_device` are set while speech is on, otherwise null.
  `applying` lists the changes still running in the background (`source`,
  `speak`, `voice_id`, `output_device`); a change that fails leaves the old
  value in place and says why in `message`.
- `line.target_language` is the language the line was translated into. A
  target change while running applies from the next committed sentence;
  earlier lines keep their language.
- `status.languages` lists every subtitle target with `speakable` for spoken
  translation. `code`, `target_language`, `source_language` and
  `detected_language` use ISO 639-1 where one exists, otherwise ISO 639-3.
- While a session runs, a status event goes out about every 2 s. While no
  session runs, `/events` sends a status heartbeat about every 3 s.
- `reset` appears only on a subscription that asked for `after` the owner no
  longer covers; the consumer drops its lines, and the events that follow hold
  every stored line.

Nothing is written to disk. The owner keeps the last 200 lines in memory.

## Routes (`/api/plugins/hermeneus/`)

| Route | Answer |
|---|---|
| `GET /status` | the latest status event |
| `GET /lines?after=<seq>` | `{"seq": <latest>, "reset": bool, "events": [...]}`. Without `after`, or with an `after` the owner does not cover (older than its buffer, or newer than its latest seq after a restart), `reset` is true and `events` holds one line event per stored line plus the status. Otherwise `events` holds the lines that changed, the relabels and the status after `after`. |
| `POST /start` | body `{"source": {"backend", "source_id"}, "target_language", "speak", "output_device": <device_id or null>, "voice_id"?}`. Answers the status event (it has a `state`). Errors: 400 missing or invalid choice, 409 a session already runs or the output device would be captured again, 422 the start failed (for example audio capture). Error bodies are `{"ok": false, "detail": "<message>", "error": "<message>"}`. |
| `POST /stop` | the status event after the stop |
| `POST /settings` | change the running session. Body: any of `{"mode": "subtitles\|both\|voice", "speak": bool, "target_language", "voice_id", "output_device", "source": {"backend", "source_id"} or a name}`. Answers at once with `{"type": "set", "ok": true, "accepted": {...}, "applying": [...], "status": {...}}`: `accepted` holds what is in effect already (target language, mode when speech stays on or turns off), `applying` what runs in the background (speech on, a new voice, a new output device, a new source). Errors: 400 unknown field or source, 409 no session or the change is refused (the body's `session` holds the status). |
| `GET /voices?refresh=` | the voices on the ElevenLabs account: `{"voices": [{"voice_id", "name", "category", "flash_languages": [...], "is_default"}], "default_voice_id", "fetched_at"}`, from `GET https://api.elevenlabs.io/v2/voices`, cached 60 s. `flash_languages` are the account's verified languages for `eleven_flash_v2_5`. Error: 502 with a plain `detail`. |
| `GET /devices?backend=&source_id=` | `{"sources": [{backend, source_id, label, detail}], "outputs": [{device_id, label}], "unavailable": [...], "outputs_note": ""}`. `outputs` are the devices that are safe for spoken translation with the named source, or with every listed source when none is named. |
| `GET /doctor` | `{"ok": bool, "checks": [{"id", "label", "state": "ok\|warn\|fail", "detail", "fix"?}]}` |
| `WS /events?after=<seq>` | `hello`, then events as above. It does not end when a session ends; it falls back to idle heartbeats and attaches to the next session. |

`voice_id` falls back to the `voice_id` plugin setting, then to the default
voice Talia (`OZ0L6eISlOejga3XjDFt`). When ElevenLabs reports the voice as
unavailable, the session switches to the default voice and the status
`message` says so.

## Unix socket

The owner listens on `<HERMES_HOME>/plugin-data/hermeneus/run/sock` (mode
0600, directory 0700, no TCP). One JSON request line per connection:
`{"op": "status"}`, `{"op": "lines", "after": <seq or null>}` (answers
`{"type": "lines", "seq", "reset", "events"}`), `{"op": "subscribe", "after":
<seq or null>}` (a stream as above), `{"op": "stop"}` (answers `{"type":
"ok"}`), `{"op": "set", "changes": {...}}` (the fields of `POST /settings`,
with `source` as the full `{backend, source_id, label, detail}`; answers the
`set` object above or `{"type": "error", "ok": false, "message"}`).

## Changes while running

| Change | In effect | How |
|---|---|---|
| target language | at once, from the next committed sentence | the session stamps each line; the scheduler translates into the line's language |
| mode `both` and `voice` while speech is on | at once | status field only; the bus is unchanged |
| speech off (`--speak off`, mode `subtitles`) | at once | nothing new is queued; the speaker closes in the background, which stops its playback and its ElevenLabs socket |
| speech on, new voice, new output device | background | a new speaker starts (playback opens after the feedback route check, the socket pre-connects), then the old one closes; when the new one fails the old one stays |
| source | background | a new capture backend starts next to the old one with a sink that drops audio, the old one stops, then audio flows; the STT session is the same. When speech is on, the route check runs against the new source first. When the new source cannot start, the old one keeps running |
