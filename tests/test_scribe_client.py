"""ScribeLiveClient against a local fake Scribe server on 127.0.0.1.

No test opens a connection outside loopback.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
from http import HTTPStatus
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from hermeneus_engine.contracts import (
    CommitStrategy,
    CommittedTranscript,
    DiarizedTranscript,
    PartialTranscript,
    SpeakersMerged,
    SttConfig,
    SttState,
    SttStatus,
)
from hermeneus_engine.stt.scribe import (
    _trailing_quiet_ms,
    FATAL_ERRORS,
    RETRYABLE_ERRORS,
    ScribeLiveClient,
    ScribeTuning,
    build_url,
)

PATH = "/v1/speech-to-text/realtime"


class Conn:
    """One accepted connection as the fake server sees it."""

    def __init__(self, ws):
        self.ws = ws
        self.path = ws.request.path
        self.headers = ws.request.headers
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.received: list[dict] = []
        self.closed_by_client = asyncio.Event()

    async def reader(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                self.received.append(msg)
                await self.inbox.put(msg)
        except ConnectionClosed:
            pass
        finally:
            self.closed_by_client.set()

    async def send(self, message_type: str, **fields):
        await self.ws.send(json.dumps({"message_type": message_type, **fields}))

    async def started(self):
        await self.send("session_started", session_id=f"sess-{id(self)}", config={})

    async def next_message(self, timeout: float = 5.0) -> dict:
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def wait_for(self, pred: Callable[[dict], bool], timeout: float = 5.0) -> dict:
        while True:
            msg = await self.next_message(timeout)
            if pred(msg):
                return msg

    def audio(self) -> bytes:
        return b"".join(base64.b64decode(m["audio_base_64"]) for m in self.received
                        if m.get("message_type") == "input_audio_chunk")

    def kill(self):
        self.ws.transport.abort()


Script = Callable[[Conn], Awaitable[None]]


async def hold_open(conn: Conn):
    await conn.started()
    await conn.closed_by_client.wait()


class FakeScribe:
    def __init__(self):
        self.conns: list[Conn] = []
        self.scripts: list[Script] = []
        self.default: Script = hold_open
        self.reject: list[Optional[int]] = []
        self.handshakes = 0

    def process_request(self, connection, request):
        index = self.handshakes
        self.handshakes += 1
        status = self.reject[index] if index < len(self.reject) else None
        if status:
            return connection.respond(HTTPStatus(status), "rejected\n")
        return None

    async def handler(self, ws):
        conn = Conn(ws)
        index = len(self.conns)
        self.conns.append(conn)
        reader = asyncio.create_task(conn.reader())
        script = self.scripts[index] if index < len(self.scripts) else self.default
        try:
            await script(conn)
        finally:
            reader.cancel()


@pytest.fixture
async def fake():
    server_state = FakeScribe()
    async with serve(server_state.handler, "127.0.0.1", 0,
                     process_request=server_state.process_request) as server:
        port = server.sockets[0].getsockname()[1]
        server_state.url = f"ws://127.0.0.1:{port}{PATH}"
        yield server_state


class Keys:
    def __init__(self, *values):
        self.values = list(values)
        self.calls: list[str] = []

    def __call__(self, name):
        self.calls.append(name)
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class Sleeps:
    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay):
        self.delays.append(delay)
        await asyncio.sleep(0)


def make_client(fake, *, keys=None, tuning=None, sleep=None, **cfg):
    defaults = dict(base_url=fake.url, commit_strategy=CommitStrategy.VAD, diarize=True,
                    multilang_mode=True, include_language_detection=False)
    defaults.update(cfg)
    counter = iter(range(1, 10**9))
    client = ScribeLiveClient(
        SttConfig(**defaults), keys or Keys("key-1"),
        tuning=tuning or ScribeTuning(drain_quiet=0.1, drain_timeout=1.0, language_wait=0.5),
        clock=lambda: next(counter) * 1000,
        sleep=sleep or Sleeps(),
        rng=lambda: 1.0,
    )
    return client


async def collect(client, until: Callable, timeout: float = 5.0) -> list:
    out = []

    async def run():
        async for ev in client.events():
            out.append(ev)
            if until(ev, out):
                return

    await asyncio.wait_for(run(), timeout)
    return out


def status_is(*states):
    return lambda ev, out: isinstance(ev, SttStatus) and ev.state in states


def statuses(events):
    return [(e.state, e.error_type) for e in events if isinstance(e, SttStatus)]


def of(events, kind):
    return [e for e in events if isinstance(e, kind)]


def pcm(marker: int, nbytes: int) -> bytes:
    return bytes([marker]) * nbytes


# ---------------------------------------------------------------- URL and header


def test_build_url_defaults():
    url = build_url(SttConfig())
    assert url == ("wss://api.elevenlabs.io/v1/speech-to-text/realtime?model_id=scribe_v3_live"
                   "&audio_format=pcm_16000&commit_strategy=vad&diarize=true&multilang_mode=true"
                   "&include_language_detection=true")


async def test_query_string_and_header_exact(fake):
    client = make_client(fake, keys=Keys("secret-abc"), commit_strategy=CommitStrategy.TURN_PREDICTION,
                         multilang_mode=False, include_language_detection=True,
                         language_code="tr", secondary_languages=("en", "de"))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    conn = fake.conns[0]
    parts = urlsplit(conn.path)
    assert parts.path == PATH
    assert parts.query == ("model_id=scribe_v3_live&audio_format=pcm_16000&commit_strategy=turn_prediction"
                           "&diarize=true&multilang_mode=false&include_language_detection=true"
                           "&language_code=tr&secondary_languages=en&secondary_languages=de")
    assert conn.headers["xi-api-key"] == "secret-abc"
    assert "secret-abc" not in conn.path
    await client.close()


# ---------------------------------------------------------------- event parsing


async def test_scripted_event_flow(fake):
    async def script(conn):
        await conn.started()
        await conn.send("partial_transcript", text="Hello wor", turn_probability=0.12,
                        words=[{"text": "Hello", "start": 0.0, "end": 0.4, "speaker_id": "speaker_0", "type": "word"},
                               {"text": " ", "type": "spacing", "speaker_id": None},
                               {"text": "wor", "speaker_id": None}])
        await conn.send("committed_transcript", text="Hello world.", segment_index=0)
        await conn.send("committed_transcript_with_timestamps", text="Hello world.", language_code="en",
                        words=[{"text": "Hello", "start": 0.0, "end": 0.4}])
        await conn.send("diarized_transcript", segment_index=0, text="Hello world.",
                        words=[{"text": "Hello", "speaker_id": "speaker_0", "start": 0.0, "end": 0.4,
                                "type": "word", "logprob": -0.1, "characters": ["H"]},
                               {"text": "world.", "speaker_id": "speaker_1"}])
        await conn.send("brand_new_event", foo=1)
        await conn.send("committed_transcript_entities", text="Hello world.", entities=[])
        await conn.send("warning", warning="just so you know")
        await conn.send("commit_throttled", error="slow down")
        await conn.send("speakers_merged", from_speaker_id="speaker_1", to_speaker_id="speaker_0")
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake, include_language_detection=True)
    await client.start()
    events = await collect(client, lambda ev, out: isinstance(ev, SpeakersMerged))
    assert statuses(events) == [(SttState.CONNECTING, ""), (SttState.LIVE, "")]
    [partial] = of(events, PartialTranscript)
    assert partial.text == "Hello wor" and partial.turn_probability == 0.12
    assert [w.speaker_id for w in partial.words] == ["s1:speaker_0", None, None]
    assert partial.words[0].start == 0.0 and partial.words[0].end == 0.4 and partial.words[1].type == "spacing"
    assert partial.received_ns > 0
    [committed] = of(events, CommittedTranscript)
    assert (committed.text, committed.segment_index, committed.language_code) == ("Hello world.", 0, "en")
    [diarized] = of(events, DiarizedTranscript)
    assert diarized.segment_index == 0
    assert [w.speaker_id for w in diarized.words] == ["s1:speaker_0", "s1:speaker_1"]
    [merged] = of(events, SpeakersMerged)
    assert (merged.from_speaker_id, merged.to_speaker_id) == ("s1:speaker_1", "s1:speaker_0")
    kinds = [type(e).__name__ for e in events if not isinstance(e, SttStatus)]
    assert kinds == ["PartialTranscript", "CommittedTranscript", "DiarizedTranscript", "SpeakersMerged"]
    stamps = [e.received_ns for e in events if not isinstance(e, SttStatus)]
    assert stamps == sorted(stamps) and all(stamps)
    await client.close()
    assert fake.conns[0].received[-1]["commit"] is True


async def test_commit_waits_for_language_then_times_out(fake):
    async def script(conn):
        await conn.started()
        await conn.send("committed_transcript", text="no language", segment_index=0)
        await conn.send("partial_transcript", text="next")
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake, include_language_detection=True,
                         tuning=ScribeTuning(language_wait=0.15, drain_quiet=0.1))
    await client.start()
    events = await collect(client, lambda ev, out: isinstance(ev, PartialTranscript))
    [committed] = of(events, CommittedTranscript)
    assert committed.language_code is None
    # The partial that arrived after the commit is not emitted ahead of it.
    assert events.index(committed) < events.index(of(events, PartialTranscript)[0])
    assert client.stats.languages_missed == 1
    await client.close()


async def test_language_event_before_its_commit_is_merged(fake):
    # Live order for a client commit:true (run1_vad_smoke.json): the language
    # event arrives 2 ms before committed_transcript.
    async def script(conn):
        await conn.started()
        await conn.send("committed_transcript_with_timestamps", text="İyi akşamlar.", language_code="tr",
                        words=None)
        await conn.send("committed_transcript", text="İyi akşamlar.", segment_index=0)
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake, include_language_detection=True,
                         tuning=ScribeTuning(language_wait=5.0, drain_quiet=0.1))
    await client.start()
    events = await collect(client, lambda ev, out: isinstance(ev, CommittedTranscript), timeout=2)
    [committed] = of(events, CommittedTranscript)
    assert (committed.text, committed.language_code) == ("İyi akşamlar.", "tr")
    assert client.stats.languages_missed == 0
    await client.close()


async def test_late_language_event_does_not_label_the_next_commit(fake):
    async def script(conn):
        await conn.started()
        await conn.send("committed_transcript", text="first", segment_index=0)
        await asyncio.sleep(0.3)  # past language_wait
        await conn.send("committed_transcript_with_timestamps", text="first", language_code="en")
        await conn.send("committed_transcript", text="second", segment_index=1)
        await conn.send("committed_transcript_with_timestamps", text="second", language_code="tr")
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake, include_language_detection=True,
                         tuning=ScribeTuning(language_wait=0.1, drain_quiet=0.1))
    await client.start()
    events = await collect(client, lambda ev, out: len(of(out, CommittedTranscript)) == 2)
    assert [(c.text, c.language_code) for c in of(events, CommittedTranscript)] == [
        ("first", None), ("second", "tr")]
    await client.close()


async def test_empty_commit_is_skipped_with_its_diarization(fake):
    async def script(conn):
        await conn.started()
        await conn.send("committed_transcript", text="", segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text="", words=[])
        await conn.send("committed_transcript", text="real", segment_index=1)
        await conn.send("diarized_transcript", segment_index=1, text="real",
                        words=[{"text": "real", "speaker_id": "speaker_0"}])
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake)
    await client.start()
    events = await collect(client, lambda ev, out: isinstance(ev, DiarizedTranscript))
    assert [(c.text, c.segment_index) for c in of(events, CommittedTranscript)] == [("real", 0)]
    assert [d.segment_index for d in of(events, DiarizedTranscript)] == [0]
    await client.close()


# ---------------------------------------------------------------- audio path


async def test_audio_chunks_from_capture_thread(fake):
    client = make_client(fake)
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    frame = 320  # 10 ms
    sent = bytearray()

    def capture():
        for i in range(100):
            data = pcm(i % 251, frame)
            sent.extend(data)
            client.send_audio(data, i)

    t = threading.Thread(target=capture)
    t.start()
    t.join()
    conn = fake.conns[0]
    while len(conn.audio()) < 32000:
        await asyncio.sleep(0.01)
    await client.close()
    chunks = [m for m in conn.received if m["message_type"] == "input_audio_chunk"]
    sizes = [len(base64.b64decode(m["audio_base_64"])) for m in chunks]
    assert sizes[:-1] == [3200] * 10 and sizes[-1] == 0
    assert all(m["sample_rate"] == 16000 for m in chunks)
    assert [m["commit"] for m in chunks] == [False] * 10 + [True]
    assert conn.audio() == bytes(sent)
    assert "previous_text" not in chunks[0]
    assert set(chunks[0]) == {"message_type", "audio_base_64", "commit", "sample_rate"}
    await asyncio.wait_for(conn.closed_by_client.wait(), 2)
    assert conn.ws.close_code == 1000


async def test_queue_drops_oldest_audio_when_full(fake):
    async def slow_start(conn):
        await asyncio.sleep(0.4)
        await conn.started()
        await conn.closed_by_client.wait()

    fake.scripts = [slow_start]
    client = make_client(fake, tuning=ScribeTuning(queue_seconds=0.1, drain_quiet=0.1))
    await client.start()
    for i in range(10):
        client.send_audio(pcm(i, 3200), i)
    await collect(client, status_is(SttState.LIVE))
    conn = fake.conns[0]
    while len(conn.audio()) < 3200:
        await asyncio.sleep(0.01)
    assert conn.audio() == pcm(9, 3200)
    assert client.stats.drop_events == 9 and client.stats.audio_bytes_dropped == 9 * 3200
    await client.close()


def test_send_audio_before_start_is_an_error():
    client = ScribeLiveClient(SttConfig(), lambda name: "k")
    with pytest.raises(RuntimeError):
        client.send_audio(b"\0\0", 0)


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize("error_type", sorted(FATAL_ERRORS))
async def test_fatal_error_stops_without_reconnect(fake, error_type):
    async def script(conn):
        await conn.started()
        await conn.send(error_type, error=f"server says {error_type}")
        await conn.ws.close()

    fake.scripts = [script]
    sleeps = Sleeps()
    client = make_client(fake, sleep=sleeps)
    await client.start()
    events = await collect(client, status_is(SttState.FAILED, SttState.CLOSED))
    final = events[-1]
    assert final.state == SttState.FAILED and final.error_type == error_type
    assert final.message
    assert len(fake.conns) == 1 and sleeps.delays == []


async def test_auth_error_before_session_started(fake):
    async def script(conn):
        await conn.send("auth_error", error="Invalid API key")
        await conn.ws.close()

    fake.scripts = [script]
    client = make_client(fake)
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert events[-1].error_type == "auth_error"
    assert "ELEVENLABS_API_KEY" in events[-1].message
    assert SttState.LIVE not in [s for s, _ in statuses(events)]


async def test_insufficient_permissions_is_fatal(fake):
    async def script(conn):
        await conn.started()
        await conn.send("auth_error", error="API key is missing the speech_to_text permission")
        await conn.ws.close()

    fake.scripts = [script]
    client = make_client(fake)
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert "permission" in events[-1].message


@pytest.mark.parametrize("error_type", sorted(RETRYABLE_ERRORS))
async def test_retryable_error_reconnects_with_rotated_key(fake, error_type):
    async def first(conn):
        await conn.started()
        await asyncio.sleep(0.1)
        await conn.send(error_type, error="try again")
        await conn.ws.close()

    fake.scripts = [first]
    keys = Keys("key-old", "key-new")
    client = make_client(fake, keys=keys)
    await client.start()
    events = await collect(client, lambda ev, out: len([e for e in out if isinstance(e, SttStatus)
                                                        and e.state == SttState.LIVE]) == 2)
    assert statuses(events) == [(SttState.CONNECTING, ""), (SttState.LIVE, ""),
                                (SttState.RECONNECTING, error_type), (SttState.LIVE, "")]
    assert [c.headers["xi-api-key"] for c in fake.conns] == ["key-old", "key-new"]
    assert keys.calls == ["ELEVENLABS_API_KEY"] * 2
    await client.close()


async def test_connection_killed_mid_stream_remaps_indices_and_speakers(fake):
    async def first(conn):
        await conn.started()
        for i, text in enumerate(["alpha", "bravo"]):
            await conn.send("committed_transcript", text=text, segment_index=i)
            await conn.send("diarized_transcript", segment_index=i, text=text,
                            words=[{"text": text, "speaker_id": "speaker_0"}])
        await conn.send("speakers_merged", from_speaker_id="speaker_1", to_speaker_id="speaker_0")
        await asyncio.sleep(0.05)
        conn.kill()

    async def second(conn):
        await conn.started()
        await conn.wait_for(lambda m: m["message_type"] == "input_audio_chunk")
        await conn.send("committed_transcript", text="charlie", segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text="charlie",
                        words=[{"text": "charlie", "speaker_id": "speaker_0"}])
        await conn.closed_by_client.wait()

    fake.scripts = [first, second]
    client = make_client(fake)
    await client.start()
    events = await collect(client, lambda ev, out: len([e for e in out if isinstance(e, SttStatus)
                                                        and e.state == SttState.LIVE]) == 2)
    client.send_audio(pcm(1, 3200), 0)
    events += await collect(client, lambda ev, out: isinstance(ev, DiarizedTranscript)
                            and ev.text == "charlie")
    assert (SttState.RECONNECTING, "connection_closed") in statuses(events)
    assert [(c.text, c.segment_index) for c in of(events, CommittedTranscript)] == [
        ("alpha", 0), ("bravo", 1), ("charlie", 2)]
    assert [(d.segment_index, d.words[0].speaker_id) for d in of(events, DiarizedTranscript)] == [
        (0, "s1:speaker_0"), (1, "s1:speaker_0"), (2, "s2:speaker_0")]
    assert [(m.from_speaker_id, m.to_speaker_id) for m in of(events, SpeakersMerged)] == [
        ("s1:speaker_1", "s1:speaker_0")]
    first_chunk = next(m for m in fake.conns[1].received if m["message_type"] == "input_audio_chunk")
    assert first_chunk["previous_text"] == "bravo"
    await client.close()


async def test_session_time_limit_reconnects_immediately_and_shortens_rotation(fake):
    async def first(conn):
        await conn.started()
        await asyncio.sleep(0.1)
        await conn.send("session_time_limit_exceeded", error="Maximum session time reached")
        await conn.ws.close()

    fake.scripts = [first]
    sleeps = Sleeps()
    client = make_client(fake, sleep=sleeps)
    await client.start()
    events = await collect(client, lambda ev, out: len([e for e in out if isinstance(e, SttStatus)
                                                        and e.state == SttState.LIVE]) == 2)
    assert (SttState.RECONNECTING, "session_time_limit_exceeded") in statuses(events)
    assert sleeps.delays == []
    assert client._rotation_seconds == 30.0
    await client.close()


async def test_idle_close_waits_for_audio_before_reconnecting(fake):
    async def first(conn):
        await conn.started()
        await asyncio.sleep(0.1)
        await conn.send("insufficient_audio_activity", error="no audio")
        await conn.ws.close()

    fake.scripts = [first]
    client = make_client(fake)
    await client.start()
    events = await collect(client, status_is(SttState.RECONNECTING))
    assert events[-1].error_type == "insufficient_audio_activity"
    await asyncio.sleep(0.3)
    assert len(fake.conns) == 1
    client.send_audio(pcm(7, 3200), 0)
    await collect(client, status_is(SttState.LIVE))
    while len(fake.conns[1].audio()) < 3200:
        await asyncio.sleep(0.01)
    assert fake.conns[1].audio() == pcm(7, 3200)
    await client.close()


async def test_http_401_is_fatal(fake):
    fake.reject = [401]
    client = make_client(fake)
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert events[-1].error_type == "http_401"


async def test_http_503_retries_with_capped_jittered_backoff_then_fails(fake):
    fake.reject = [503] * 10
    sleeps = Sleeps()
    client = make_client(fake, sleep=sleeps,
                         tuning=ScribeTuning(max_attempts=4, backoff_base=0.5, backoff_cap=2.0))
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert sleeps.delays == [0.5, 1.0, 2.0, 2.0]  # rng fixed at 1.0, so each delay is its ceiling
    assert fake.handshakes == 5
    assert "after 4 attempts" in events[-1].message
    assert events[-1].error_type == "http_503"


async def test_http_503_then_success(fake):
    fake.reject = [503]
    client = make_client(fake)
    await client.start()
    events = await collect(client, status_is(SttState.LIVE))
    assert statuses(events) == [(SttState.CONNECTING, ""), (SttState.RECONNECTING, "http_503"),
                                (SttState.LIVE, "")]
    await client.close()


async def test_missing_key_fails_without_connecting(fake):
    client = make_client(fake, keys=Keys(None))
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert events[-1].error_type == "missing_key"
    assert fake.handshakes == 0


async def test_key_is_redacted_from_errors_and_logs(fake, caplog):
    key = "sk_live_supersecret_123"

    async def leaky_connect(url, headers):
        raise OSError(f"connect failed for {url} with key {headers['xi-api-key']}")

    caplog.set_level(logging.DEBUG, logger="hermeneus.stt.scribe")
    client = ScribeLiveClient(SttConfig(base_url=fake.url), Keys(key), connect=leaky_connect,
                              tuning=ScribeTuning(max_attempts=2), sleep=Sleeps())
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    text = " ".join(e.message for e in events if isinstance(e, SttStatus))
    assert key not in text and "[redacted]" in text
    assert key not in caplog.text


async def test_server_error_text_is_redacted(fake, caplog):
    key = "sk_echoed_key_456"

    async def script(conn):
        await conn.started()
        await conn.send("invalid_request", error=f"bad key {key}")
        await conn.ws.close()

    fake.scripts = [script]
    caplog.set_level(logging.DEBUG, logger="hermeneus.stt.scribe")
    client = make_client(fake, keys=Keys(key))
    await client.start()
    events = await collect(client, status_is(SttState.FAILED))
    assert key not in events[-1].message and "[redacted]" in events[-1].message
    assert key not in caplog.text


# ---------------------------------------------------------------- rotation


async def test_rotation_without_a_pause_keeps_order_without_loss(fake):
    async def old(conn):
        await conn.started()
        # Natural VAD commit some time after the rotation interval has passed.
        await asyncio.sleep(0.5)
        await conn.send("committed_transcript", text="one", segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text="one",
                        words=[{"text": "one", "speaker_id": "speaker_0"}])
        await conn.wait_for(lambda m: m.get("commit") is True)
        await asyncio.sleep(0.3)  # the new session commits before this flush answer arrives
        await conn.send("committed_transcript", text="two", segment_index=1)
        await conn.send("diarized_transcript", segment_index=1, text="two",
                        words=[{"text": "two", "speaker_id": "speaker_1"}])
        await conn.send("partial_transcript", text="stale tail")
        await conn.closed_by_client.wait()

    async def new(conn):
        await conn.started()
        await conn.wait_for(lambda m: m["message_type"] == "input_audio_chunk")
        await conn.send("committed_transcript", text="three", segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text="three",
                        words=[{"text": "three", "speaker_id": "speaker_0"}])
        await conn.send("partial_transcript", text="fresh")
        await conn.closed_by_client.wait()

    fake.scripts = [old, new]
    client = make_client(fake, tuning=ScribeTuning(rotation_seconds=0.2, boundary_wait=1.0,
                                                   drain_quiet=0.5, drain_timeout=3.0,
                                                   segment_soft_seconds=0, segment_hard_seconds=0))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    # Session 1 keeps its 0.2 s rotation deadline; session 2 gets a long one so
    # the test observes exactly one rotation.
    client._rotation_seconds = 60.0

    sent = bytearray()
    stop = asyncio.Event()

    async def feed():
        i = 0
        while not stop.is_set():
            data = pcm(i % 251, 3200)
            sent.extend(data)
            client.send_audio(data, i)
            i += 1
            await asyncio.sleep(0.02)

    feeder = asyncio.create_task(feed())
    events = await collect(client, lambda ev, out: len(of(out, DiarizedTranscript)) == 3, timeout=8)
    stop.set()
    await feeder
    await client.close()

    assert [(c.text, c.segment_index) for c in of(events, CommittedTranscript)] == [
        ("one", 0), ("two", 1), ("three", 2)]
    assert [(d.segment_index, d.words[0].speaker_id) for d in of(events, DiarizedTranscript)] == [
        (0, "s1:speaker_0"), (1, "s1:speaker_1"), (2, "s2:speaker_0")]
    assert "stale tail" not in [p.text for p in of(events, PartialTranscript)]
    assert statuses(events) == []  # rotation emits no status: it is invisible to the user
    assert client.stats.rotations == 1
    # Every audio byte reached exactly one session, in order.
    assert fake.conns[0].audio() + fake.conns[1].audio() == bytes(sent)
    flush = [m for m in fake.conns[0].received if m.get("commit") is True]
    assert len(flush) == 1 and base64.b64decode(flush[0]["audio_base_64"]) == b""
    first_new = next(m for m in fake.conns[1].received if m["message_type"] == "input_audio_chunk")
    assert first_new["previous_text"] == "one"
    assert fake.conns[0].closed_by_client.is_set()


async def test_close_while_reconnecting_ends_cleanly(fake):
    fake.reject = [503] * 10
    gate = asyncio.Event()

    async def slow_sleep(delay):
        await gate.wait()

    client = make_client(fake, sleep=slow_sleep)
    await client.start()
    await collect(client, status_is(SttState.RECONNECTING))
    await asyncio.wait_for(client.close(), 2)
    events = await collect(client, status_is(SttState.CLOSED, SttState.FAILED))
    assert events[-1].state == SttState.CLOSED


# ---------------------------------------------------------------- segment length cap


def loud(nbytes: int) -> bytes:
    return (8000).to_bytes(2, "little", signed=True) * (nbytes // 2)


def test_trailing_quiet_run():
    quiet_100ms = bytes(3200)
    assert _trailing_quiet_ms(quiet_100ms, 0, -40.0) == 100
    assert _trailing_quiet_ms(quiet_100ms, 70, -40.0) == 170
    assert _trailing_quiet_ms(loud(3200), 500, -40.0) == 0
    assert _trailing_quiet_ms(loud(1600) + bytes(1600), 500, -40.0) == 50


def test_build_url_with_vad_silence_threshold():
    url = build_url(SttConfig(), ScribeTuning(vad_silence_threshold_secs=0.2))
    assert url.endswith("&include_language_detection=true&vad_silence_threshold_secs=0.2")


def commit_flags(conn):
    return [m["commit"] for m in conn.received if m["message_type"] == "input_audio_chunk"]


async def feed_and_close(client, conn, chunks):
    total = len(conn.audio())
    for chunk in chunks:
        client.send_audio(chunk, 0)
        total += len(chunk)
    while len(conn.audio()) < total:
        await asyncio.sleep(0.01)
    await client.close()


async def test_pause_commit_after_soft_limit(fake):
    client = make_client(fake, tuning=ScribeTuning(segment_soft_seconds=0.3, segment_hard_seconds=0,
                                                   pause_ms=120, drain_quiet=0.1))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    # 0.4 s of speech, then 0.2 s of silence, then speech again.
    await feed_and_close(client, fake.conns[0], [loud(3200)] * 4 + [bytes(3200)] * 2 + [loud(3200)] * 2)
    flags = commit_flags(fake.conns[0])
    # The commit follows the second quiet chunk (200 ms quiet >= 120 ms), then the close commit.
    assert flags == [False] * 6 + [True] + [False] * 2 + [True]
    assert client.stats.pause_commits == 1 and client.stats.hard_commits == 0


async def test_hard_commit_without_pause(fake):
    client = make_client(fake, tuning=ScribeTuning(segment_soft_seconds=0, segment_hard_seconds=0.5,
                                                   drain_quiet=0.1))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    await feed_and_close(client, fake.conns[0], [loud(3200)] * 7)
    assert commit_flags(fake.conns[0]) == [False] * 5 + [True] + [False] * 2 + [True]
    assert client.stats.hard_commits == 1


async def test_server_commit_resets_segment_length(fake):
    async def script(conn):
        await conn.started()
        for _ in range(4):
            await conn.wait_for(lambda m: m["message_type"] == "input_audio_chunk")
        await conn.send("committed_transcript", text="vad commit", segment_index=0)
        await conn.closed_by_client.wait()

    fake.scripts = [script]
    client = make_client(fake, tuning=ScribeTuning(segment_soft_seconds=0, segment_hard_seconds=0.6,
                                                   drain_quiet=0.1))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    for _ in range(4):
        client.send_audio(loud(3200), 0)
    await collect(client, lambda ev, out: isinstance(ev, CommittedTranscript))
    await feed_and_close(client, fake.conns[0], [loud(3200)] * 4)
    # 0.8 s sent in total, but never 0.6 s since the server commit.
    assert commit_flags(fake.conns[0]) == [False] * 8 + [True]
    assert client.stats.hard_commits == 0


async def test_rotation_switches_at_a_pause_in_sent_audio(fake):
    client = make_client(fake, tuning=ScribeTuning(rotation_seconds=0.2, boundary_wait=5.0, pause_ms=120,
                                                   drain_quiet=0.2, segment_soft_seconds=0,
                                                   segment_hard_seconds=0))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    client._rotation_seconds = 60.0
    plan = [loud(3200)] * 8 + [bytes(3200)] * 2 + [loud(3200)] * 4
    for chunk in plan:
        client.send_audio(chunk, 0)
        await asyncio.sleep(0.05)
    while len(fake.conns) < 2 or len(fake.conns[0].audio()) + len(fake.conns[1].audio()) < 14 * 3200:
        await asyncio.sleep(0.01)
    await client.close()
    # The old session got everything up to and including the 200 ms pause; the new one starts after it.
    assert fake.conns[0].audio() == b"".join(plan[:10])
    assert fake.conns[1].audio() == b"".join(plan[10:])
    assert client.stats.rotations == 1


# ---------------------------------------------------------------- bounded close


def answer_final_commit(delay: float = 0.1, hold_close: float = 0.0, text: str = "tail words"):
    async def script(conn):
        await conn.started()
        await conn.wait_for(lambda m: m.get("commit") is True)
        await asyncio.sleep(delay)
        await conn.send("committed_transcript", text=text, segment_index=0)
        await conn.send("committed_transcript_with_timestamps", text=text, language_code="en")
        await conn.send("diarized_transcript", segment_index=0, text=text,
                        words=[{"text": text, "speaker_id": "speaker_0"}])
        if hold_close:
            # Stop reading, so the client's close frame goes unanswered for a while.
            conn.ws.transport.pause_reading()
            await asyncio.sleep(hold_close)
            conn.ws.transport.resume_reading()
        await conn.closed_by_client.wait()
    return script


async def test_close_returns_once_the_final_commit_is_answered(fake):
    fake.scripts = [answer_final_commit(delay=0.1)]
    client = make_client(fake, include_language_detection=True,
                         tuning=ScribeTuning(drain_quiet=2.0, close_budget=3.0))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    client.send_audio(loud(3200), 0)
    started = asyncio.get_running_loop().time()
    await client.close(timeout=3.0)
    elapsed = asyncio.get_running_loop().time() - started
    events = await collect(client, status_is(SttState.CLOSED))
    assert elapsed < 0.6  # no drain_quiet wait after the answer
    [committed] = of(events, CommittedTranscript)
    assert (committed.text, committed.language_code) == ("tail words", "en")
    assert events.index(committed) < len(events) - 1
    assert set(client.stats.close_ms) >= {"audio sent", "final commit sent", "socket closed", "closed"}
    await asyncio.wait_for(fake.conns[0].closed_by_client.wait(), 2)
    assert fake.conns[0].ws.close_code == 1000


async def test_close_is_bounded_when_the_server_delays_its_close_frame(fake):
    fake.scripts = [answer_final_commit(delay=0.2, hold_close=3.0)]
    client = make_client(fake, include_language_detection=True,
                         tuning=ScribeTuning(drain_quiet=0.5, close_timeout=2.0))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    client.send_audio(loud(3200), 0)
    started = asyncio.get_running_loop().time()
    await client.close(timeout=1.0)
    assert len(fake.conns[0].audio()) == 3200  # audio queued just before close() was sent
    elapsed = asyncio.get_running_loop().time() - started
    events = await collect(client, status_is(SttState.CLOSED))
    assert elapsed <= 1.1
    # Text that arrived inside the budget is emitted before CLOSED.
    assert [c.text for c in of(events, CommittedTranscript)] == ["tail words"]
    assert events[-1].state == SttState.CLOSED
    assert "forced" not in client.stats.close_ms
    # The client sent its close frame with code 1000 before giving up on the reply.
    await asyncio.wait_for(fake.conns[0].closed_by_client.wait(), 5)
    assert fake.conns[0].ws.close_code == 1000


async def test_close_is_bounded_when_the_server_never_answers(fake):
    async def silent(conn):
        await conn.started()
        await conn.wait_for(lambda m: m.get("commit") is True)
        conn.ws.transport.pause_reading()
        await asyncio.sleep(3)
        conn.ws.transport.resume_reading()
        await conn.closed_by_client.wait()

    fake.scripts = [silent]
    client = make_client(fake, tuning=ScribeTuning(drain_quiet=5.0, close_timeout=5.0))
    await client.start()
    await collect(client, status_is(SttState.LIVE))
    started = asyncio.get_running_loop().time()
    await client.close(timeout=0.5)
    elapsed = asyncio.get_running_loop().time() - started
    events = await collect(client, status_is(SttState.CLOSED))
    assert elapsed <= 0.6 and events[-1].state == SttState.CLOSED


async def test_close_while_connecting_is_bounded(fake):
    async def slow(conn):
        await asyncio.sleep(3)
        await conn.started()
        await conn.closed_by_client.wait()

    fake.scripts = [slow]
    client = make_client(fake)
    await client.start()
    await asyncio.sleep(0.1)
    started = asyncio.get_running_loop().time()
    await client.close(timeout=0.4)
    elapsed = asyncio.get_running_loop().time() - started
    events = await collect(client, status_is(SttState.CLOSED))
    assert elapsed <= 0.5 and events[-1].state == SttState.CLOSED
    assert "forced" in client.stats.close_ms
