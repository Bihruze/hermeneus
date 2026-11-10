"""Fake pipeline stages with known delays, for testing the latency harness.

Delays run on one timer thread with the time constraint policy, so an
injected delay of N ms takes N ms plus tens of microseconds. asyncio timers
in this process overshoot by several ms on macOS (see the W11 report), which
would hide harness measurement error behind timer slack.
"""

from __future__ import annotations

import asyncio
import heapq
import importlib.util
import itertools
import sys
import threading
import time
from array import array
from pathlib import Path
from typing import Callable, Optional

from hermeneus_engine import contracts as c
from hermeneus_engine import metrics as m

ROOT = Path(__file__).resolve().parents[1]


_LOAD_LOCK = threading.Lock()


def load_harness():
    """The harness module, loaded once from its file. Timer threads may call this concurrently;
    the lock keeps them out until the module has fully executed."""
    name = "hermeneus_latency_harness"
    with _LOAD_LOCK:
        mod = sys.modules.get(name)
        if mod is None and __import__("__main__").__dict__.get("promote_realtime"):
            mod = sys.modules["__main__"]  # running under the harness CLI
        if mod is None:
            spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "latency_harness.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod  # dataclasses look the module up while it executes
            try:
                spec.loader.exec_module(mod)
            except BaseException:
                del sys.modules[name]
                raise
        return mod


class PreciseTimer:
    """Runs callbacks at monotonic deadlines on one high priority thread."""

    def __init__(self):
        self._heap: list = []
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="fake-timer", daemon=True)
        self._thread.start()

    def at(self, deadline_ns: int, fn: Callable[[], None]) -> None:
        with self._cond:
            heapq.heappush(self._heap, (deadline_ns, next(self._seq), fn))
            self._cond.notify()

    def after_ms(self, delay_ms: float, fn: Callable[[], None]) -> None:
        self.at(time.monotonic_ns() + int(delay_ms * 1e6), fn)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify()

    def _run(self) -> None:
        load_harness().promote_realtime(5_000_000, 1_000_000, 2_000_000)
        while True:
            fn = None
            with self._cond:
                while True:
                    if self._closed:
                        return
                    if not self._heap:
                        self._cond.wait()
                        continue
                    remaining = self._heap[0][0] - time.monotonic_ns()
                    if remaining <= 0:
                        _, _, fn = heapq.heappop(self._heap)
                        break
                    if remaining > 2_000_000:
                        self._cond.wait((remaining - 1_000_000) / 1e9)
                        continue
                    break
            if fn is None:
                time.sleep(max(0, remaining) / 1e9)
                continue
            fn()


class FakeStt:
    """Energy detector standing in for Scribe.

    A burst starts on the first chunk with a nonzero sample and ends on the
    first fully silent chunk after it. The partial is emitted ``partial_ms``
    after the start chunk arrives, the commit ``commit_ms[strategy]`` after
    the silent chunk arrives, and the diarized transcript ``diarized_ms``
    after the commit. Word timestamps are exact sample positions in seconds.
    """

    def __init__(self, config: c.SttConfig, on_event, secrets, *, partial_ms: float = 40,
                 commit_ms: Optional[dict] = None, diarized_ms: float = 30, with_partial_words: bool = True,
                 reconnect_after_bursts: Optional[int] = None, emit_diarized: bool = True):
        self.config = config
        self.on_event = on_event
        self.partial_ms = partial_ms
        self.commit_ms = commit_ms or {c.CommitStrategy.VAD: 300, c.CommitStrategy.TURN_PREDICTION: 120}
        self.diarized_ms = diarized_ms
        self.with_partial_words = with_partial_words
        self.reconnect_after = reconnect_after_bursts
        self.emit_diarized = emit_diarized
        self.reconnect_pending = False
        self.timer: Optional[PreciseTimer] = None
        self.samples = 0
        self.in_burst = False
        self.burst_start = 0
        self.last_loud = 0
        self.index = 0
        self.arrivals: list[dict] = []
        self.closed = False

    async def start(self) -> None:
        self.timer = PreciseTimer()
        self.on_event(c.SttStatus(c.SttState.CONNECTING))
        self.on_event(c.SttStatus(c.SttState.LIVE))

    def _emit(self, ev_fn):
        def fire():
            if not self.closed:
                self.on_event(ev_fn())
        return fire

    def send_audio(self, pcm: bytes, capture_ns: int) -> None:
        now = time.monotonic_ns()
        a = array("h")
        a.frombytes(pcm)
        loud = [i for i, v in enumerate(a) if v != 0]
        sr = c.ENGINE_SAMPLE_RATE
        if loud and not self.in_burst:
            self.in_burst = True
            self.burst_start = self.samples + loud[0]
            start_s = self.burst_start / sr
            words = (c.Word("tone", start=start_s, end=start_s + 0.01, type="word"),) if self.with_partial_words else ()
            self.arrivals.append({"start_chunk_arrival_ns": now, "start_sample": self.burst_start})
            self.timer.after_ms(self.partial_ms, self._emit(
                lambda: c.PartialTranscript("tone", words=words, received_ns=time.monotonic_ns())))
        if loud:
            self.last_loud = self.samples + loud[-1] + 1
        elif self.in_burst:
            self.in_burst = False
            idx = self.index
            self.index += 1
            start_s, end_s = self.burst_start / sr, self.last_loud / sr
            self.arrivals[-1].update(end_chunk_arrival_ns=now, end_sample=self.last_loud)
            delay = self.commit_ms[self.config.commit_strategy]
            self.timer.after_ms(delay, self._emit(lambda: c.CommittedTranscript(
                f"tone {idx}", segment_index=idx, language_code="en", received_ns=time.monotonic_ns())))
            words = (c.Word("tone", start=start_s, end=end_s, speaker_id="speaker_0", type="word"),
                     c.Word(" ", start=end_s, end=end_s + 0.5, type="spacing"))
            if self.emit_diarized:
                self.timer.after_ms(delay + self.diarized_ms, self._emit(
                    lambda: c.DiarizedTranscript(idx, f"tone {idx}", words, received_ns=time.monotonic_ns())))
            if self.reconnect_after is not None and self.index == self.reconnect_after:
                self.timer.after_ms(delay + self.diarized_ms + 5, self._request_reconnect)
        self.samples += len(a)
        if self.reconnect_pending and not self.in_burst:
            # New server session after this chunk: the next chunk is stream sample 0.
            self.reconnect_pending = False
            self.on_event(c.SttStatus(c.SttState.RECONNECTING, "fake drop"))
            self.on_event(c.SttStatus(c.SttState.LIVE))
            self.samples = 0

    def _request_reconnect(self) -> None:
        self.reconnect_pending = True

    async def close(self) -> None:
        self.closed = True
        if self.timer:
            self.timer.close()
        self.on_event(c.SttStatus(c.SttState.CLOSED))

    def stats(self) -> dict:
        return {"dropped_chunks": 0, "bursts": self.index}


def stt_factory(**kw):
    instances = []

    def make(config, on_event, secrets):
        s = FakeStt(config, on_event, secrets, **kw)
        instances.append(s)
        return s

    make.instances = instances
    return make


class FakeTranslator:
    def __init__(self, delay_ms: float, fail_on: str = ""):
        self.delay_ms = delay_ms
        self.fail_on = fail_on
        self.timer = PreciseTimer()
        self.calls: list[dict] = []

    async def translate(self, text, *, source_language, target_language, context):
        if self.fail_on and self.fail_on in text:
            raise c.TranslationError("fake failure")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        call_ns = time.monotonic_ns()
        fired = []

        def fire():
            fired.append(time.monotonic_ns())
            loop.call_soon_threadsafe(fut.set_result, None)

        self.timer.after_ms(self.delay_ms, fire)
        await fut
        self.calls.append({"text": text, "context": tuple(context), "target": target_language,
                           "call_ns": call_ns, "fired_ns": fired[0], "resumed_ns": time.monotonic_ns()})
        return text.upper()


class FakeSpeaker:
    """Marks first audio ``first_audio_ms`` after speak() starts and playback ``playback_ms`` after that,
    both from the timer thread, the way a real speaker reports from its audio callback."""

    def __init__(self, first_audio_ms: float, playback_ms: float):
        self.first_audio_ms = first_audio_ms
        self.playback_ms = playback_ms
        self.timer = PreciseTimer()
        self.spoken: list[str] = []

    async def speak(self, text, *, language):
        first = m.stage_marker(m.Stage.TTS_FIRST_AUDIO)
        play = m.stage_marker(m.Stage.PLAYBACK_START)
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        t0 = time.monotonic_ns()

        def fire_first():
            first()

        def fire_play():
            play()
            loop.call_soon_threadsafe(done.set_result, None)

        self.timer.at(t0 + int(self.first_audio_ms * 1e6), fire_first)
        self.timer.at(t0 + int((self.first_audio_ms + self.playback_ms) * 1e6), fire_play)
        await done
        self.spoken.append(text)

    async def close(self):
        self.timer.close()


class FakeCapture:
    """CaptureBackend producing a tone burst pattern at real time; ``skip`` lists chunk numbers to drop."""

    name = "fake-capture"

    def __init__(self, chunk_ms: int = 10, skip: tuple[int, ...] = (), burst_chunks: int = 30,
                 gap_chunks: int = 40):
        self.chunk = c.ENGINE_SAMPLE_RATE * chunk_ms // 1000
        self.skip = set(skip)
        self.burst_chunks = burst_chunks
        self.gap_chunks = gap_chunks
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stop_calls = 0

    def available(self):
        return True, "fake"

    def list_sources(self):
        return [c.CaptureSource(self.name, "fake-0", "Fake source")]

    def start(self, source, sink):
        def run():
            load_harness().promote_realtime(10_000_000, 2_000_000, 5_000_000)
            tone = array("h", [1000] * self.chunk).tobytes()
            silence = bytes(self.chunk * 2)
            t0 = time.monotonic_ns()
            period = self.chunk * 1_000_000_000 // c.ENGINE_SAMPLE_RATE
            for i in itertools.count():
                if self._stop.is_set():
                    return
                deliver = t0 + (i + 1) * period
                r = deliver - time.monotonic_ns()
                if r > 0:
                    time.sleep(r / 1e9)
                if i in self.skip:
                    continue
                phase = i % (self.burst_chunks + self.gap_chunks)
                sink(tone if phase < self.burst_chunks else silence, t0 + i * period)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_calls += 1
        self._stop.set()
        if self._thread:
            self._thread.join(2)


# Module level factories for the CLI (``--stt tests.fakes:fake_stt``).
fake_stt = stt_factory()


def fake_translator(secrets):
    return FakeTranslator(150)


def fake_speaker(secrets):
    return FakeSpeaker(80, 50)


def fake_capture():
    return FakeCapture()
