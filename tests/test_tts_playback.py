import numpy as np
import pytest

from hermeneus_engine.contracts import FeedbackRiskError
from hermeneus_engine.tts.playback import PlaybackError, PlaybackQueue, UtteranceState
from hermeneus_engine.tts.routing import CaptureGate, RouteVerdict

from fake_audio import DEVICES, FakeClock, fake_factory

SR = 24000


def pcm(value, n):
    return np.full(n, value, dtype="<i2").tobytes()


def make(clock=None, **kw):
    clock = clock or FakeClock()
    q = PlaybackQueue("Test Built-in Speakers", sample_rate=SR, stream_factory=fake_factory,
                      query_devices=lambda: DEVICES, clock=clock, **kw)
    q.start()
    return q, q._stream, clock


def test_opens_named_device_stereo_int16():
    q, s, _ = make()
    assert s.kwargs["device"] == 1 and s.kwargs["channels"] == 2
    assert s.kwargs["samplerate"] == SR and s.kwargs["dtype"] == "int16" and s.started


def test_mono_device_gets_one_channel():
    q = PlaybackQueue("Test Mono Out", stream_factory=fake_factory, query_devices=lambda: DEVICES)
    q.start()
    assert q._stream.kwargs["channels"] == 1


def test_unknown_device_raises_plain_error():
    q = PlaybackQueue("Nope", stream_factory=fake_factory, query_devices=lambda: DEVICES)
    with pytest.raises(PlaybackError, match="Available outputs"):
        q.start()


def test_plays_in_open_order_even_if_audio_arrives_out_of_order():
    q, s, _ = make()
    a = q.open_utterance("a", q._clock())
    b = q.open_utterance("b", q._clock())
    q.feed(b, pcm(2, 100)); q.finish(b)          # b's audio arrives first
    assert not s.pull(50).any()                  # a has no audio yet: silence, b waits
    q.feed(a, pcm(1, 100)); q.finish(a)
    out = s.pull(250)
    assert (out[:100] == 1).all() and (out[100:200] == 2).all() and (out[200:] == 0).all()
    assert out.shape == (250, 2)
    assert a.state is UtteranceState.DONE and b.state is UtteranceState.DONE


def test_streaming_utterance_continues_across_chunks():
    q, s, _ = make()
    a = q.open_utterance("a", q._clock())
    q.feed(a, pcm(1, 30))
    out = s.pull(60)
    assert (out[:30] == 1).all() and (out[30:] == 0).all()   # underrun inside an utterance is silence
    q.feed(a, pcm(3, 30)); q.finish(a)
    assert (s.pull(30) == 3).all()


def test_drift_policy_skips_stale_utterance_and_keeps_started_one():
    clock = FakeClock()
    q, s, _ = make(clock=clock, max_start_lag_s=3.0)
    long = q.open_utterance("long", clock())
    q.feed(long, pcm(1, SR * 5)); q.finish(long)   # 5 s of speech
    late = q.open_utterance("late", clock())
    q.feed(late, pcm(2, 100)); q.finish(late)
    s.pull(SR)                      # long starts
    clock.advance(4.0)              # long still playing when late passes 3 s
    out = s.pull(SR * 4 + 200)      # rest of long, then late would start
    assert (out[: SR * 4] == 1).all()               # started utterance not cut
    assert (out[SR * 4:] == 0).all()                # late was skipped
    assert late.state is UtteranceState.DROPPED
    kinds = [(e.kind, e.utterance_id) for e in q.drain_events()]
    assert ("dropped", "late") in kinds and ("done", "long") in kinds


def test_drift_policy_skips_utterance_whose_audio_never_came_in_time():
    clock = FakeClock()
    q, s, _ = make(clock=clock, max_start_lag_s=3.0)
    slow = q.open_utterance("slow", clock())
    nxt = q.open_utterance("next", clock())
    q.feed(nxt, pcm(5, 10)); q.finish(nxt)
    s.pull(10)
    clock.advance(3.5)
    out = s.pull(10)
    assert slow.state is UtteranceState.DROPPED
    # next is also older than 3 s by now and is skipped by the same rule
    assert nxt.state is UtteranceState.DROPPED and not out.any()


def test_backlog_seconds_counts_unplayed_audio():
    q, s, _ = make()
    a = q.open_utterance("a", q._clock())
    q.feed(a, pcm(1, SR))
    b = q.open_utterance("b", q._clock())
    q.feed(b, pcm(1, SR // 2))
    assert q.backlog_seconds() == pytest.approx(1.5)
    s.pull(SR // 2)
    assert q.backlog_seconds() == pytest.approx(1.0)


def test_cancel_and_stop():
    q, s, _ = make()
    a = q.open_utterance("a", q._clock())
    q.feed(a, pcm(1, 100)); q.cancel(a)
    b = q.open_utterance("b", q._clock())
    q.feed(b, pcm(2, 100))
    out = s.pull(100)
    assert (out == 2).all() and a.state is UtteranceState.CANCELLED
    q.stop()
    assert s.stopped and s.closed and b.state is UtteranceState.CANCELLED and not q.running
    q.stop()  # second call is a no-op


def test_route_check_blocks_start_unless_gate_mode():
    risky = RouteVerdict(True, "same-device", "loop", "BlackHole 2ch", "BlackHole 2ch", "virtual-device")
    q = PlaybackQueue("BlackHole 2ch", stream_factory=fake_factory, query_devices=lambda: DEVICES,
                      route_check=lambda: risky)
    with pytest.raises(FeedbackRiskError, match="loop"):
        q.start()
    assert not q.running
    with pytest.raises(ValueError):
        PlaybackQueue("BlackHole 2ch", gate_mode=True)
    gate = CaptureGate()
    q2 = PlaybackQueue("BlackHole 2ch", stream_factory=fake_factory, query_devices=lambda: DEVICES,
                       route_check=lambda: risky, gate_mode=True, gate=gate)
    q2.start()
    assert q2.running and q2.verdict is risky


def test_gate_marks_audible_span_at_dac_time():
    clock = FakeClock(start_ns=5_000_000_000)
    gate = CaptureGate(tail_s=0.25)
    q, s, _ = make(clock=clock, gate=gate)
    a = q.open_utterance("a", clock())
    q.feed(a, pcm(1, SR // 10)); q.finish(a)   # 100 ms
    s.pull(SR // 10, dac_offset_s=0.03)
    start = 5_000_000_000 + 30_000_000
    assert gate.is_gated(start + 50_000_000, start + 60_000_000)
    assert gate.is_gated(start + 300_000_000, start + 310_000_000)       # inside tail
    assert not gate.is_gated(start + 400_000_000, start + 410_000_000)   # past tail
    s.pull(100)  # silence does not extend the span
    assert not gate.is_gated(start + 400_000_000, start + 410_000_000)
