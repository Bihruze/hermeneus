"""The live path end to end: the real ScribeLiveClient and the real session against a local fake Scribe.

The A1 run on real audio found that no line carried speech_end_ms and no
sentence was released at a pause, while the session tests with fake STT
passed: the fakes carried no word times and bypassed the audio consumer the
silence gate swaps in. This test sends audio through the session's
FrameQueue, the real client and a websocket, and the fake server answers
with timed words, across a reconnect, as Scribe does.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from websockets.asyncio.server import serve

from pipeline_fakes import make_rig, options, remove_tmpdir, short_tmpdir
from test_scribe_client import PATH, FakeScribe, Keys

from hermeneus_engine.contracts import CommitStrategy, LineState, SttConfig
from hermeneus_engine.runtime import SessionRuntime
from hermeneus_engine.stt.scribe import ScribeLiveClient, ScribeTuning

CHUNK = bytes([16, 0]) * 320  # 20 ms, quiet but not digital silence
BYTES_PER_S = 32000


def timed(text: str, start: float, step: float = 0.15) -> list[dict]:
    words, t = [], start
    for i, w in enumerate(text.split()):
        if i:
            words.append({"text": " ", "start": t - 0.02, "end": t, "type": "spacing", "speaker_id": "speaker_0"})
        words.append({"text": w, "start": t, "end": t + step * 0.8, "type": "word", "speaker_id": "speaker_0"})
        t += step
    return words


async def wait_audio(conn, seconds: float, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while len(conn.audio()) < seconds * BYTES_PER_S:
        if time.monotonic() > deadline:
            raise AssertionError("the fake server did not receive enough audio")
        await asyncio.sleep(0.01)


@pytest.fixture
async def server():
    state = FakeScribe()
    async with serve(state.handler, "127.0.0.1", 0, process_request=state.process_request) as srv:
        state.url = f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}{PATH}"
        yield state


async def test_real_client_and_session_map_word_times_and_release_at_a_pause(server):
    first = "The council approved the plan."
    second = "Rain is expected on Sunday evening."
    sent_before_second: dict = {}

    async def session_one(conn):
        await conn.started()
        await wait_audio(conn, 1.0)
        await conn.send("partial_transcript", text=first, words=timed(first, 0.1))
        await wait_audio(conn, 2.2)
        await conn.send("committed_transcript", text=first, segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text=first, words=timed(first, 0.1))
        await asyncio.sleep(0.1)
        sent_before_second["bytes"] = len(conn.audio())
        await conn.ws.close()  # the server ends the session; the client reconnects

    async def session_two(conn):
        await conn.started()
        await wait_audio(conn, 1.0)
        await conn.send("partial_transcript", text=second, words=timed(second, 0.1))
        await wait_audio(conn, 2.2)
        await conn.send("committed_transcript", text=second, segment_index=0)
        await conn.send("diarized_transcript", segment_index=0, text=second, words=timed(second, 0.1))
        await conn.closed_by_client.wait()

    server.scripts = [session_one, session_two]

    async def no_backoff(_delay):
        await asyncio.sleep(0)

    client = ScribeLiveClient(
        SttConfig(base_url=server.url, commit_strategy=CommitStrategy.VAD, diarize=True,
                  include_language_detection=False),
        Keys("key-1"), tuning=ScribeTuning(drain_quiet=0.1, drain_timeout=1.0, language_wait=0.5),
        sleep=no_backoff, rng=lambda: 1.0, stream_times=True)
    data_dir = short_tmpdir()
    rig = make_rig(stt=client)
    rt = SessionRuntime(data_dir, options(sentence_pause_ms=300), rig.build, owner="test")
    pushed: list[int] = []  # capture time of each 20 ms chunk, in stream order
    try:
        await rt.start()
        session = rt.session
        t0 = time.monotonic_ns()
        for i in range(400):  # 8 s of audio in real time
            due = t0 + i * 20_000_000
            await asyncio.sleep(max(0.0, (due - time.monotonic_ns()) / 1e9))
            session._frames.put(CHUNK, due)
            pushed.append(due)
            lines = {l.original: l for l in rt.bus.lines()}
            if all(t in lines and "speech_end" in lines[t].timings_ns for t in (first, second)):
                break

        lines = {l.original: l for l in rt.bus.lines()}
        assert session.stats["sentences_released_at_pause"] == 2
        one, two = lines[first], lines[second]
        assert one.state == LineState.TRANSLATED and two.state == LineState.TRANSLATED
        assert "commit" not in one.timings_ns  # released at the pause, before Scribe's commit

        def capture_at(stream_s: float) -> int:
            return pushed[0] + round(stream_s * 1e9)

        last_end = 0.1 + 4 * 0.15 + 0.15 * 0.8  # end of the fifth word
        assert abs(one.timings_ns["speech_end"] - capture_at(last_end)) < 25_000_000
        # The second Scribe session's word times count from its own first sample;
        # with stream_times they are shifted by everything sent before it.
        offset_s = sent_before_second["bytes"] / BYTES_PER_S
        last_end_two = offset_s + 0.1 + 5 * 0.15 + 0.15 * 0.8
        assert abs(two.timings_ns["speech_end"] - capture_at(last_end_two)) < 25_000_000

        events, _ = rt.bus.events_after(None)
        timings = {e["line"]["original"]: e["timings"] for e in events if e["type"] == "line"}
        assert {"capture_ms", "speech_end_ms", "released_ms", "translated_ms"} <= set(timings[second])
        summary = session.latency_summary()
        assert summary["end_of_sentence_to_subtitle"]["count"] == 2
        assert summary["end_of_sentence_to_released"]["count"] == 2
        assert summary["released_to_translated"]["count"] == 2
    finally:
        await rt.close()
        remove_tmpdir(data_dir)
