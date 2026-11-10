import asyncio
import random
import threading
import time

import numpy as np
import pytest

from hermeneus_engine import contracts as c
from hermeneus_engine import metrics as m


def test_percentile_matches_numpy_linear():
    rng = random.Random(3)
    for n in (1, 2, 5, 101, 1000):
        data = sorted(rng.uniform(0, 500) for _ in range(n))
        for q in (0, 50, 90, 95, 99, 100):
            assert m.percentile(data, q) == pytest.approx(float(np.percentile(data, q)), abs=1e-9)


def test_distribution_empty_and_values():
    assert m.distribution([]) == {"count": 0}
    d = m.distribution([3.0, 1.0, 2.0])
    assert (d["count"], d["min"], d["p50"], d["max"], d["mean"]) == (3, 1.0, 2.0, 3.0, 2.0)


def test_mark_first_wins_set_overwrites():
    r = m.LatencyRecorder()
    assert r.mark("a", m.Stage.COMMIT, 10)
    assert not r.mark("a", m.Stage.COMMIT, 20)
    r.set("a", m.Stage.CAPTURE, 5)
    r.set("a", m.Stage.CAPTURE, 6)
    assert r.get("a") == {m.Stage.COMMIT: 10, m.Stage.CAPTURE: 6}


def test_eviction_keeps_newest():
    r = m.LatencyRecorder(max_segments=3)
    for k in range(5):
        r.mark(k, m.Stage.COMMIT, k)
        r.annotate(k, text=str(k))
    snap = r.snapshot()
    assert [k for k, _, _ in snap] == [2, 3, 4]
    assert r.evicted == 2
    assert snap[0][2] == {"text": "2"}


def test_summary_intervals_end_to_end_and_flags():
    r = m.LatencyRecorder()
    ms = 1_000_000
    r.mark(1, m.Stage.CAPTURE, 0)
    r.mark(1, m.Stage.SPEECH_END, 400 * ms)
    r.mark(1, m.Stage.PARTIAL, 100 * ms)
    r.mark(1, m.Stage.COMMIT, 700 * ms)
    r.mark(1, m.Stage.TRANSLATED, 900 * ms)
    r.mark(2, m.Stage.COMMIT, 1000 * ms)              # no capture: unattributed
    r.mark(3, m.Stage.CAPTURE, 2000 * ms)
    r.mark(3, m.Stage.PARTIAL, 1990 * ms)             # partial before capture: negative
    s = r.summary(end_stage=m.Stage.TRANSLATED)
    iv = s["intervals"]
    assert iv["capture_to_committed"]["p50"] == 700.0
    assert iv["speech_end_to_committed"]["p50"] == 300.0
    assert iv["committed_to_translated"]["p50"] == 200.0
    assert iv["end_to_end"]["p50"] == 900.0 and iv["end_to_end"]["to"] == m.Stage.TRANSLATED
    assert iv["capture_to_first_partial"]["count"] == 2
    assert iv["capture_to_first_partial"]["negative"] == 1
    assert iv["translated_to_tts_first_audio"] == {"count": 0, "from": "translated",
                                                   "to": "tts_first_audio", "negative": 0}
    assert s["unattributed"] == 1 and s["segments"] == 3


def test_record_timings_from_subtitle_line():
    line = c.SubtitleLine("l1", c.LineState.TRANSLATED, "hi",
                          timings_ns={"capture": 1, "commit": 2, "translated": 3, "tts_first_audio": 4,
                                      "unknown": 9, "bad": "x"})
    r = m.LatencyRecorder()
    r.mark("l1", m.Stage.COMMIT, 99)
    r.record_timings("l1", line.timings_ns)
    assert r.get("l1") == {"capture": 1, "commit": 99, "translated": 3, "tts_first_audio": 4}


def test_threads_mark_concurrently():
    r = m.LatencyRecorder(max_segments=100000)
    wins = []
    barrier = threading.Barrier(8)

    def work(t):
        barrier.wait()
        for i in range(2000):
            r.mark((t, i), m.Stage.COMMIT, i)
        wins.append(r.mark("shared", m.Stage.COMMIT, t))

    threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(r.snapshot()) == 8 * 2000 + 1
    assert wins.count(True) == 1


async def test_bind_segment_reaches_tasks_and_threads():
    r = m.LatencyRecorder()
    assert m.mark_current(m.Stage.TTS_FIRST_AUDIO) is False
    assert m.stage_marker(m.Stage.PLAYBACK_START) is None
    with m.bind_segment(r, "k"):
        async def child():
            await asyncio.sleep(0)
            return m.mark_current(m.Stage.TTS_FIRST_AUDIO, 5)

        assert await asyncio.create_task(child())
        marker = m.stage_marker(m.Stage.PLAYBACK_START)
    th = threading.Thread(target=marker, args=(7,))
    th.start()
    th.join()
    assert m.mark_current(m.Stage.COMMIT) is False
    assert r.get("k") == {"tts_first_audio": 5, "playback_start": 7}


def test_stream_clock_maps_across_capture_gap_and_session():
    clk = m.StreamClock()
    assert clk.to_capture_ns(0.0) is None
    base = 1_000_000_000_000
    ms = 1_000_000
    # 20 ms chunks; capture skips 100 ms after the second chunk.
    clk.on_frame(640, base)
    clk.on_frame(640, base + 20 * ms)
    clk.on_frame(640, base + 140 * ms)
    assert clk.to_capture_ns(0.010) == (base + 10 * ms, False)
    assert clk.to_capture_ns(0.030) == (base + 30 * ms, False)
    # Stream time 45 ms is 5 ms into the third chunk, which was captured after the gap.
    assert clk.to_capture_ns(0.045) == (base + 145 * ms, False)
    assert clk.to_capture_ns(0.070)[1] is True
    assert clk.to_capture_ns(-1) is None
    clk.new_session()
    assert clk.to_capture_ns(0.045) == (base + 145 * ms, False)  # reset applies at the next frame
    clk.on_frame(640, base + 500 * ms)
    assert clk.to_capture_ns(0.005) == (base + 505 * ms, False)
    assert clk.sessions == 2 and clk.samples_sent == 320


def test_timed_span_ignores_spacing_and_untimed():
    words = (c.Word(" ", start=0.0, end=0.1, type="spacing"), c.Word("a", start=0.2, end=0.4),
             c.Word("b"), c.Word("c", start=0.5, type="word"))
    assert m.timed_span(words) == (0.2, 0.5)
    assert m.timed_span((c.Word("x"),)) is None


def test_mark_overhead_is_small():
    r = m.LatencyRecorder(max_segments=50000)
    n = 50000
    t = time.perf_counter_ns()
    for i in range(n):
        r.mark(i, m.Stage.COMMIT)
    per = (time.perf_counter_ns() - t) / n
    assert per < 5000, per  # measured about 240 ns on the dev Mac; 5 us leaves room for a loaded CI box
