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
{"type": "status", "seq": 44, "state": "live", "message": "", "source": {"backend": "virtual-device", "source_id": "BlackHole 2ch", "label": "BlackHole 2ch"}, "target_language": "tr", "detected_language": "en", "speak": false, "metrics": {}, "languages": [{"code": "tr", "name": "Turkish", "speakable": true}]}
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
  `speak_errors`, `sentences_released_early`, `translation_backlog`,
  `uptime_s`). When translations fail, `message` names the reason and the
  fix once, again when it changes, and at most every 30 s otherwise; it is
  cleared by the next translated line.
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
"ok"}`), `{"op": "overlay", "on": <true, false or null>, "port": <int or null>}`
(switches the overlay page, null only asks; answers `{"type": "overlay",
"running", "url", "port", "error"}`).

## Overlay page

With the overlay page on, the session owner serves `http://127.0.0.1:<port>/<token>/`
and `GET /<token>/events`, a Server-Sent Events stream. Each SSE `data:` line
is one event from the list above, cut down to what the page draws: `hello`
and `status` keep only `state`, `line` keeps `line_id`, `state`, `original`,
`translation` and `speaker_id`; `relabel` and `reset` are unchanged. Line,
relabel and status events carry their `seq` as the SSE `id`, so a reconnect
resumes from `Last-Event-ID`. A first connection gets every stored line
(`after` 0). The stream reads the session through `Controller.subscribe`.
