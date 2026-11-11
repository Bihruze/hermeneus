#!/usr/bin/env python3
"""Hermeneus latency harness.

Drives a pipeline built from the engine contracts (capture or WAV replay,
speech to text, translation, speech output), records per-segment stage
timestamps with ``hermeneus_engine.metrics`` and writes ``summary.json`` and
``summary.md`` to an output directory. Run ``latency_harness.py --help`` and
``latency_harness.py run --help`` for usage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ctypes
import datetime as _dt
import hashlib
import importlib
import itertools
import json
import math
import os
import random
import resource
import signal
import stat
import subprocess
import sys
import threading
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence, runtime_checkable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermeneus_engine import contracts as c  # noqa: E402
from hermeneus_engine import metrics as m  # noqa: E402
from hermeneus_engine.components import SECRET_NAME  # noqa: E402

DEFAULT_OUT = Path.home() / "Desktop" / "agent-reports" / "hermeneus" / "gates"
G1_MIN_SECONDS = 600.0
NS = 1_000_000_000


# --------------------------------------------------------------------------
# Adapter protocol for speech to text
# --------------------------------------------------------------------------

@runtime_checkable
class SttSession(Protocol):
    """What the harness needs from an STT client. A thin adapter wraps the real client.

    The factory is called as ``factory(config, on_event, secrets)`` where
    ``on_event(event)`` accepts any ``SttEvent`` from any thread.
    ``send_audio`` has the ``FrameSink`` signature and is called from the
    audio thread. An optional ``stats()`` returning a dict (for example
    ``{"dropped_chunks": 0, "reconnects": 1}``) is copied into the summary.
    """

    async def start(self) -> None: ...

    def send_audio(self, pcm: bytes, capture_ns: int) -> None: ...

    async def close(self) -> None: ...


SttFactory = Callable[[c.SttConfig, Callable[[Any], None], c.SecretGetter], SttSession]


def load_spec(spec: str) -> Any:
    """Import ``package.module:attribute`` and return the attribute."""
    if ":" not in spec:
        raise ValueError(f"expected module:attribute, got {spec!r}")
    mod_name, attr = spec.split(":", 1)
    obj: Any = importlib.import_module(mod_name)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def no_secrets(name: str) -> Optional[str]:
    """The default secret getter. Fake-backed runs need no key, and the harness never reads
    credentials from the process environment."""
    return None


class KeyFileSecrets:
    """Serves the ElevenLabs key from a file the user names with ``--key-file``.

    The file must be a regular file owned by the user, readable by nobody else (mode 600 or
    stricter), outside the repository, and hold only the key. A ``NAME=value`` file such as a
    Hermes ``.env`` is refused: the harness does not parse Hermes secret stores. Every other
    secret name resolves to None. The value never appears in ``repr`` or in the summary.
    """

    def __init__(self, path: str | os.PathLike, *, name: str = SECRET_NAME, repo_root: Path = ROOT):
        p = Path(path).expanduser()
        try:
            st = p.stat()
        except OSError as exc:
            raise SystemExit(f"--key-file {p}: {exc.strerror}") from None
        if not stat.S_ISREG(st.st_mode):
            raise SystemExit(f"--key-file {p}: not a regular file")
        if st.st_uid != os.getuid():
            raise SystemExit(f"--key-file {p}: owned by another user")
        if st.st_mode & 0o077:
            raise SystemExit(f"--key-file {p}: readable by other users; run chmod 600 {p}")
        resolved = p.resolve()
        root = Path(repo_root).resolve()
        if resolved == root or root in resolved.parents:
            raise SystemExit(f"--key-file {p}: inside the repository; keep keys outside it")
        text = p.read_text(encoding="utf-8").strip()
        if not text or any(ch.isspace() for ch in text) or "=" in text:
            raise SystemExit(f"--key-file {p}: must hold only the key, on one line, with no NAME= prefix")
        self.name = name
        self.path = resolved
        self._value = text

    def __call__(self, name: str) -> Optional[str]:
        return self._value if name == self.name else None

    def __repr__(self) -> str:
        return f"KeyFileSecrets({self.name} from {self.path})"


# --------------------------------------------------------------------------
# Timing helpers
# --------------------------------------------------------------------------

def promote_realtime(period_ns: int, computation_ns: int, constraint_ns: int) -> str:
    """Give the calling thread the macOS time constraint policy used by audio IO threads.

    Without it, sleeps in this process overshoot by about 3.5 ms (timer
    coalescing); with it the overshoot is tens of microseconds. Returns the
    policy in effect, for the summary.
    """
    if sys.platform != "darwin":
        return "default (not macOS)"
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")

        class _Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        class _TimeConstraint(ctypes.Structure):
            _fields_ = [("period", ctypes.c_uint32), ("computation", ctypes.c_uint32),
                        ("constraint", ctypes.c_uint32), ("preemptible", ctypes.c_int)]

        tb = _Timebase()
        lib.mach_timebase_info(ctypes.byref(tb))

        def to_abs(ns: int) -> int:
            return ns * tb.denom // tb.numer

        lib.mach_thread_self.restype = ctypes.c_uint32
        policy = _TimeConstraint(to_abs(period_ns), to_abs(computation_ns), to_abs(constraint_ns), 1)
        thread_time_constraint_policy, count = 2, 4
        rc = lib.thread_policy_set(lib.mach_thread_self(), thread_time_constraint_policy,
                                   ctypes.byref(policy), count)
    except (OSError, AttributeError) as exc:
        return f"default ({exc})"
    return "time_constraint" if rc == 0 else f"default (thread_policy_set returned {rc})"


def sleep_until(deadline_ns: int) -> None:
    remaining = deadline_ns - time.monotonic_ns()
    if remaining > 0:
        time.sleep(remaining / NS)


def _ms(values_ns: Sequence[int]) -> dict:
    return m.distribution(v / 1e6 for v in values_ns)


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

def read_engine_wav(path: Path) -> bytes:
    """PCM frames of a WAV that is already in engine format. Other formats are refused."""
    with wave.open(str(path), "rb") as w:
        fmt = (w.getframerate(), w.getsampwidth(), w.getnchannels())
        want = (c.ENGINE_SAMPLE_RATE, c.ENGINE_SAMPLE_WIDTH, c.ENGINE_CHANNELS)
        if fmt != want:
            raise SystemExit(
                f"{path}: {fmt[0]} Hz, {fmt[1] * 8} bit, {fmt[2]} channel(s); the harness replays "
                f"{want[0]} Hz 16 bit mono only. Convert first, for example: "
                f"afconvert -f WAVE -d LEI16@16000 -c 1 in.wav out.wav")
        return w.readframes(w.getnframes())


class PacedReplay:
    """Replays PCM through a FrameSink at real time on its own thread.

    Chunk ``i`` covers samples ``[i*n, (i+1)*n)``. Its capture timestamp is the
    virtual time of its first sample, ``t0 + i*n/sr``, and it is delivered
    when its last sample would have been captured, ``t0 + (i+1)*n/sr``, the
    way a capture backend delivers a filled buffer. Deadlines are absolute, so
    late wakeups never accumulate into drift.
    """

    def __init__(self, pcm: bytes, sink: c.FrameSink, *, chunk_ms: int, duration_s: Optional[float],
                 loop: bool, lead_in_ms: int = 50):
        self.sample_rate = c.ENGINE_SAMPLE_RATE
        self.chunk_samples = self.sample_rate * chunk_ms // 1000
        if self.chunk_samples <= 0:
            raise ValueError("chunk_ms too small")
        self.pcm = pcm
        self.sink = sink
        self.duration_s = duration_s
        self.loop = loop
        self.lead_in_ns = lead_in_ms * 1_000_000
        self.t0_ns = 0
        self.wav_samples = len(pcm) // c.ENGINE_SAMPLE_WIDTH
        self.lateness_ns = array("q")
        self.sink_ns = array("q")
        self.chunks = 0
        self.samples = 0
        self.policy = ""
        self.error: Optional[BaseException] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hermeneus-replay", daemon=True)
        self.started = threading.Event()

    def start(self) -> None:
        self._thread.start()
        self.started.wait()

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    @property
    def done(self) -> bool:
        return not self._thread.is_alive()

    def _chunks(self):
        width = c.ENGINE_SAMPLE_WIDTH
        step = self.chunk_samples * width
        limit = None if self.duration_s is None else int(self.duration_s * self.sample_rate)
        sent = 0
        while True:
            for pos in range(0, len(self.pcm), step):
                chunk = self.pcm[pos:pos + step]
                if limit is not None:
                    room = limit - sent
                    if room <= 0:
                        return
                    chunk = chunk[:room * width]
                sent += len(chunk) // width
                yield chunk
            if not self.loop or not self.pcm:
                return

    def _run(self) -> None:
        period = self.chunk_samples * NS // self.sample_rate
        self.policy = promote_realtime(period, min(period // 4, 2_000_000), min(period // 2, 5_000_000))
        self.t0_ns = time.monotonic_ns() + self.lead_in_ns
        self.started.set()
        sr = self.sample_rate
        offset = 0
        try:
            for chunk in self._chunks():
                if self._stop.is_set():
                    break
                n = len(chunk) // c.ENGINE_SAMPLE_WIDTH
                capture_ns = self.t0_ns + offset * NS // sr
                deliver_ns = self.t0_ns + (offset + n) * NS // sr
                sleep_until(deliver_ns)
                before = time.monotonic_ns()
                self.lateness_ns.append(before - deliver_ns)
                self.sink(chunk, capture_ns)
                self.sink_ns.append(time.monotonic_ns() - before)
                offset += n
                self.chunks += 1
                self.samples = offset
        except BaseException as exc:  # reported in the summary, the run stops cleanly
            self.error = exc

    def report(self) -> dict:
        return {
            "source": "wav",
            "chunk_ms": self.chunk_samples * 1000 / self.sample_rate,
            "chunks": self.chunks,
            "audio_s": self.samples / self.sample_rate,
            "thread_policy": self.policy,
            "lateness_ms": _ms(self.lateness_ns),
            "late_over_2ms": sum(1 for v in self.lateness_ns if v > 2_000_000),
            "late_over_5ms": sum(1 for v in self.lateness_ns if v > 5_000_000),
            "sink_call_ms": _ms(self.sink_ns),
            "error": repr(self.error) if self.error else "",
        }


class FrameAccounting:
    """The sink the source feeds. Detects gaps, feeds the StreamClock, forwards to STT."""

    def __init__(self, clock: m.StreamClock, forward: Optional[c.FrameSink], gap_tolerance_ms: float):
        self.clock = clock
        self.forward = forward
        self.tolerance_ns = int(gap_tolerance_ms * 1_000_000)
        self.chunks = 0
        self.samples = 0
        self.first_capture_ns: Optional[int] = None
        self.gaps = 0
        self.gap_ns_total = 0
        self.gap_ns_max = 0
        self.overlaps = 0
        self.odd_sizes = 0
        self.forward_errors = 0
        self.last_forward_error = ""
        self._expected_ns: Optional[int] = None
        self._lock = threading.Lock()

    def __call__(self, pcm: bytes, capture_ns: int) -> None:
        nbytes = len(pcm)
        with self._lock:
            if nbytes % c.ENGINE_SAMPLE_WIDTH:
                self.odd_sizes += 1
            n = nbytes // c.ENGINE_SAMPLE_WIDTH
            if self._expected_ns is None:
                self.first_capture_ns = capture_ns
            else:
                delta = capture_ns - self._expected_ns
                if delta > self.tolerance_ns:
                    self.gaps += 1
                    self.gap_ns_total += delta
                    self.gap_ns_max = max(self.gap_ns_max, delta)
                elif delta < -self.tolerance_ns:
                    self.overlaps += 1
            self._expected_ns = capture_ns + n * NS // c.ENGINE_SAMPLE_RATE
            self.chunks += 1
            self.samples += n
        self.clock.on_frame(nbytes, capture_ns)
        if self.forward is not None:
            try:
                self.forward(pcm, capture_ns)
            except Exception as exc:  # counted as a dropped chunk, the stream continues
                with self._lock:
                    self.forward_errors += 1
                    self.last_forward_error = repr(exc)

    def report(self) -> dict:
        return {
            "chunks": self.chunks,
            "audio_s": self.samples / c.ENGINE_SAMPLE_RATE,
            "gaps": self.gaps,
            "gap_ms_total": self.gap_ns_total / 1e6,
            "gap_ms_max": self.gap_ns_max / 1e6,
            "overlaps": self.overlaps,
            "odd_size_chunks": self.odd_sizes,
            "dropped_chunks_at_stt_send": self.forward_errors,
            "last_send_error": self.last_forward_error,
        }


# --------------------------------------------------------------------------
# Process sampling
# --------------------------------------------------------------------------

def parse_ps_time(text: str) -> float:
    """Seconds from ps ``time`` output: ``[dd-][hh:]mm:ss.ss``."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    secs = 0.0
    for part in text.split(":"):
        secs = secs * 60 + float(part)
    return days * 86400 + secs


class _LibProc:
    """Process RSS and CPU time from macOS libproc, without spawning a process.

    A ``ps`` call per second forks the harness, which delayed one replay chunk
    by up to 18 ms in a 30 s run (0.04 ms max without it). ``proc_pidinfo``
    costs about 5 microseconds per sample.
    """

    class _TaskInfo(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "virtual_size", "resident_size", "total_user", "total_system", "threads_user",
            "threads_system")] + [(n, ctypes.c_int32) for n in (
                "policy", "faults", "pageins", "cow_faults", "messages_sent", "messages_received",
                "syscalls_mach", "syscalls_unix", "csw", "threadnum", "numrunning", "priority")]

    PROC_PIDTASKINFO = 4

    def __init__(self):
        self.lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        system = ctypes.CDLL("/usr/lib/libSystem.dylib")

        class _Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        tb = _Timebase()
        system.mach_timebase_info(ctypes.byref(tb))
        self.numer, self.denom = tb.numer, tb.denom
        self.buf = (ctypes.c_int * 1024)()

    def info(self, pid: int) -> Optional[tuple[int, float]]:
        """(rss bytes, cpu seconds) or None when the process is gone."""
        ti = self._TaskInfo()
        size = ctypes.sizeof(ti)
        if self.lib.proc_pidinfo(pid, self.PROC_PIDTASKINFO, ctypes.c_uint64(0), ctypes.byref(ti), size) != size:
            return None
        ticks = ti.total_user + ti.total_system  # mach absolute time units
        return ti.resident_size, ticks * self.numer / self.denom / 1e9

    def children(self, pid: int) -> list[int]:
        n = self.lib.proc_listchildpids(pid, self.buf, ctypes.sizeof(self.buf))
        return list(self.buf[:max(0, n)])


class ResourceSampler:
    """Samples CPU percent and RSS every ``interval_s`` on a background thread.

    ``self`` values cover the harness process (CPU from ``getrusage``). ``tree``
    values add live descendant processes, for example a capture helper
    binary. On macOS both come from libproc; elsewhere one ``ps -A`` per sample.
    """

    def __init__(self, interval_s: float = 1.0, pid: Optional[int] = None):
        self.interval_s = interval_s
        self.pid = pid or os.getpid()
        self.samples: list[dict] = []
        self.probe_ns = array("q")
        self.errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hermeneus-sampler", daemon=True)
        self._libproc: Optional[_LibProc] = None
        if sys.platform == "darwin":
            with contextlib.suppress(OSError, AttributeError):
                self._libproc = _LibProc()
        self.method = "libproc" if self._libproc else "ps"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(5)

    def _tree(self) -> tuple[int, float, int, int]:
        """(self rss kB, tree cpu seconds, tree rss kB, process count)."""
        t = time.monotonic_ns()
        try:
            if self._libproc is not None:
                return self._libproc_tree()
            return self._ps_tree()
        finally:
            self.probe_ns.append(time.monotonic_ns() - t)

    def _libproc_tree(self) -> tuple[int, float, int, int]:
        lp = self._libproc
        own = lp.info(self.pid)
        if own is None:
            raise OSError("proc_pidinfo failed for the harness process")
        rss, cpu, count = 0, 0.0, 0
        todo = [self.pid]
        while todo:
            p = todo.pop()
            got = own if p == self.pid else lp.info(p)
            if got is None:
                continue
            rss += got[0]
            cpu += got[1]
            count += 1
            todo.extend(lp.children(p))
        return own[0] // 1024, cpu, rss // 1024, count

    def _ps_tree(self) -> tuple[int, float, int, int]:
        proc = subprocess.Popen(["ps", "-A", "-o", "pid=,ppid=,rss=,time="], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        out, _ = proc.communicate(timeout=5)
        if proc.returncode:
            raise subprocess.SubprocessError(f"ps exited with {proc.returncode}")
        rows = {}
        children: dict[int, list[int]] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) != 4:
                continue
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            if pid == proc.pid:
                continue  # the ps call itself
            rows[pid] = (rss, parse_ps_time(parts[3]))
            children.setdefault(ppid, []).append(pid)
        tree, todo = [], [self.pid]
        while todo:
            p = todo.pop()
            if p in rows:
                tree.append(p)
            todo.extend(children.get(p, ()))
        self_rss = rows.get(self.pid, (0, 0.0))[0]
        rss = sum(rows[p][0] for p in tree)
        cpu = sum(rows[p][1] for p in tree)
        return self_rss, cpu, rss, len(tree)

    def _run(self) -> None:
        start = time.monotonic()
        last_wall = start
        ru = resource.getrusage(resource.RUSAGE_SELF)
        last_cpu = ru.ru_utime + ru.ru_stime
        last_tree_cpu: Optional[float] = None
        next_at = start + self.interval_s
        while not self._stop.wait(max(0.0, next_at - time.monotonic())):
            next_at += self.interval_s
            now = time.monotonic()
            ru = resource.getrusage(resource.RUSAGE_SELF)
            cpu = ru.ru_utime + ru.ru_stime
            sample = {"t_s": round(now - start, 3),
                      "cpu_self_pct": round((cpu - last_cpu) / (now - last_wall) * 100, 2)}
            try:
                self_rss, tree_cpu, tree_rss, nproc = self._tree()
                sample["rss_self_kb"] = self_rss
                sample["rss_tree_kb"] = tree_rss
                sample["processes"] = nproc
                if last_tree_cpu is not None:
                    sample["cpu_tree_pct"] = round((tree_cpu - last_tree_cpu) / (now - last_wall) * 100, 2)
                last_tree_cpu = tree_cpu
            except (subprocess.SubprocessError, OSError, ValueError):
                self.errors += 1
            self.samples.append(sample)
            last_cpu, last_wall = cpu, now

    def report(self) -> dict:
        def col(name):
            return [s[name] for s in self.samples if name in s]

        rss = col("rss_self_kb")
        return {
            "interval_s": self.interval_s,
            "count": len(self.samples),
            "cpu_self_pct": m.distribution(col("cpu_self_pct")),
            "cpu_tree_pct": m.distribution(col("cpu_tree_pct")),
            "rss_self_mb": m.distribution(v / 1024 for v in rss),
            "rss_tree_mb": m.distribution(v / 1024 for v in col("rss_tree_kb")),
            "rss_self_growth_mb": (rss[-1] - rss[0]) / 1024 if len(rss) > 1 else 0.0,
            "method": self.method,
            "probe_call_ms": _ms(self.probe_ns),
            "errors": self.errors,
        }


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

@dataclass
class RunConfig:
    label: str
    wav: Optional[Path] = None
    manifest: Optional[Path] = None
    capture: Any = None                  # spec string or factory returning a CaptureBackend
    capture_source: Optional[str] = None
    stt: Any = None                      # spec string or SttFactory, None for audio only
    commit_strategy: c.CommitStrategy = c.CommitStrategy.VAD
    language_code: Optional[str] = None
    translator: Any = None               # spec string or factory(secrets) -> Translator
    speaker: Any = None                  # spec string or factory(secrets) -> Speaker
    source_language: Optional[str] = None
    target_language: str = "en"
    chunk_ms: int = 20
    duration_s: Optional[float] = None
    loop: bool = False
    drain_s: float = 5.0
    sample_interval_s: float = 1.0
    gap_tolerance_ms: float = 2.0
    context_segments: int = 3
    gate: Optional[str] = None
    secrets: c.SecretGetter = no_secrets
    extra: dict = field(default_factory=dict)

    def describe(self) -> dict:
        def name(x):
            if x is None:
                return None
            return x if isinstance(x, str) else f"{getattr(x, '__module__', '?')}:{getattr(x, '__qualname__', repr(x))}"

        return {
            "label": self.label,
            "source": "wav" if self.wav else "capture",
            "wav": str(self.wav) if self.wav else None,
            "manifest": str(self.manifest) if self.manifest else None,
            "capture": name(self.capture),
            "capture_source": self.capture_source,
            "stt": name(self.stt),
            "commit_strategy": self.commit_strategy.value,
            "translator": name(self.translator),
            "speaker": name(self.speaker),
            "target_language": self.target_language,
            "chunk_ms": self.chunk_ms,
            "duration_s": self.duration_s,
            "loop": self.loop,
            "gate": self.gate,
            "secrets": "key-file" if isinstance(self.secrets, KeyFileSecrets) else "none",
        }


def _resolve(x: Any) -> Any:
    return load_spec(x) if isinstance(x, str) else x


class SegmentTracker:
    """Turns STT events into segments and drives translation and speech for each one."""

    def __init__(self, cfg: RunConfig, recorder: m.LatencyRecorder, clock: m.StreamClock,
                 translator: Optional[c.Translator], speaker: Optional[c.Speaker]):
        self.cfg = cfg
        self.recorder = recorder
        self.clock = clock
        self.translator = translator
        self.speaker = speaker
        self.open_key: Optional[int] = None
        self.last_words: dict[int, tuple] = {}
        self.by_index: dict[int, int] = {}
        self.history: list[str] = []
        self.keys = itertools.count(1)
        self.tasks: set[asyncio.Task] = set()
        self.tts_queue: "asyncio.Queue[tuple[int, str]]" = asyncio.Queue()
        self.tts_worker: Optional[asyncio.Task] = None
        self.tts_pending = 0
        self.counts = {"partials": 0, "committed": 0, "diarized": 0, "diarized_unmatched": 0,
                       "speakers_merged": 0, "translation_failures": 0, "tts_failures": 0,
                       "reconnects": 0, "status_failed": 0, "extrapolated_words": 0}
        self.states: list[tuple[float, str, str]] = []
        self._state: Optional[c.SttState] = None
        if speaker is not None:
            self.tts_worker = asyncio.get_running_loop().create_task(self._speak_loop())

    def _attribute(self, key: int, words: tuple, source: str, overwrite: bool) -> None:
        span = m.timed_span(words)
        if span is None:
            return
        start = self.clock.to_capture_ns(span[0])
        end = self.clock.to_capture_ns(span[1])
        if start is None or end is None:
            return
        if start[1] or end[1]:
            self.counts["extrapolated_words"] += 1
        if overwrite:
            self.recorder.set(key, m.Stage.CAPTURE, start[0])
            self.recorder.set(key, m.Stage.SPEECH_END, end[0])
        else:
            self.recorder.mark(key, m.Stage.CAPTURE, start[0])
            self.recorder.mark(key, m.Stage.SPEECH_END, end[0])
        self.recorder.annotate(key, attribution=source, stream_start_s=span[0], stream_end_s=span[1])

    def handle(self, ev: Any, arrived_ns: int) -> None:
        ts = getattr(ev, "received_ns", 0) or arrived_ns
        if isinstance(ev, c.PartialTranscript):
            self.counts["partials"] += 1
            if self.open_key is None:
                self.open_key = next(self.keys)
            self.recorder.mark(self.open_key, m.Stage.PARTIAL, ts)
            if ev.words:
                self.last_words[self.open_key] = ev.words
        elif isinstance(ev, c.CommittedTranscript):
            self.counts["committed"] += 1
            key = self.open_key if self.open_key is not None else next(self.keys)
            self.open_key = None
            self.recorder.mark(key, m.Stage.COMMIT, ts)
            words = self.last_words.pop(key, ())
            if words:
                self._attribute(key, words, "partial", overwrite=False)
            if ev.segment_index is not None:
                self.by_index[ev.segment_index] = key
            self.recorder.annotate(key, text=ev.text, segment_index=ev.segment_index,
                                   language=ev.language_code)
            if self.translator is not None and ev.text.strip():
                context = tuple(self.history[-self.cfg.context_segments:]) if self.cfg.context_segments else ()
                task = asyncio.get_running_loop().create_task(
                    self._translate(key, ev.text, ev.language_code, context))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
            self.history.append(ev.text)
            del self.history[:-max(1, self.cfg.context_segments)]
        elif isinstance(ev, c.DiarizedTranscript):
            self.counts["diarized"] += 1
            key = self.by_index.get(ev.segment_index)
            if key is None:
                self.counts["diarized_unmatched"] += 1
            elif ev.words:
                self._attribute(key, ev.words, "diarized", overwrite=True)
        elif isinstance(ev, c.SpeakersMerged):
            self.counts["speakers_merged"] += 1
        elif isinstance(ev, c.SttStatus):
            now = time.monotonic_ns()
            self.states.append((now / 1e9, ev.state.value, ev.message))
            if ev.state is c.SttState.RECONNECTING and self._state is not c.SttState.RECONNECTING:
                self.counts["reconnects"] += 1
            if ev.state is c.SttState.FAILED:
                self.counts["status_failed"] += 1
            self._state = ev.state

    async def _translate(self, key: int, text: str, language: Optional[str], context: tuple) -> None:
        self.recorder.mark(key, m.Stage.TRANSLATE_START)
        try:
            out = await self.translator.translate(
                text, source_language=language or self.cfg.source_language,
                target_language=self.cfg.target_language, context=context)
        except Exception as exc:  # the run continues; failures are counted
            self.counts["translation_failures"] += 1
            self.recorder.annotate(key, translation_error=repr(exc))
            return
        self.recorder.mark(key, m.Stage.TRANSLATED)
        self.recorder.annotate(key, translation=out)
        if self.speaker is not None and out.strip():
            self.tts_pending += 1
            await self.tts_queue.put((key, out))

    async def _speak_loop(self) -> None:
        while True:
            key, text = await self.tts_queue.get()
            try:
                with m.bind_segment(self.recorder, key):
                    await self.speaker.speak(text, language=self.cfg.target_language)
            except Exception as exc:  # the run continues; failures are counted
                self.counts["tts_failures"] += 1
                self.recorder.annotate(key, tts_error=repr(exc))
            finally:
                self.tts_pending -= 1

    def idle(self) -> bool:
        return self.open_key is None and not self.tasks and self.tts_pending == 0

    async def close(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        if self.tts_worker is not None:
            self.tts_worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.tts_worker


def end_stage_for(cfg: RunConfig) -> str:
    if cfg.speaker is not None:
        return m.Stage.PLAYBACK_START
    if cfg.translator is not None:
        return m.Stage.TRANSLATED
    return m.Stage.COMMIT


def load_manifest(path: Path) -> list[tuple[float, float]]:
    data = json.loads(path.read_text())
    return [(float(s["start_s"]), float(s["end_s"])) for s in data["segments"]]


def attribution_error(segments, truth: list[tuple[float, float]], t0_ns: int, wav_s: float,
                      audio_s: float) -> dict:
    """Mapped capture time minus ground truth onset, nearest truth segment, in ms."""
    if not truth:
        return {"count": 0}
    onsets, ends = [], []
    reps = max(1, math.ceil(audio_s / wav_s)) if wav_s > 0 else 1
    for k in range(reps):
        for s, e in truth:
            onsets.append(t0_ns + int((k * wav_s + s) * NS))
            ends.append(t0_ns + int((k * wav_s + e) * NS))
    start_err, end_err = [], []
    for _, seg, _ in segments:
        cap = seg.get(m.Stage.CAPTURE)
        if cap is None:
            continue
        i = min(range(len(onsets)), key=lambda j: abs(onsets[j] - cap))
        start_err.append((cap - onsets[i]) / 1e6)
        if m.Stage.SPEECH_END in seg:
            end_err.append((seg[m.Stage.SPEECH_END] - ends[i]) / 1e6)
    return {"start_ms": m.distribution(start_err), "end_ms": m.distribution(end_err),
            "abs_start_ms_max": max((abs(v) for v in start_err), default=None),
            "abs_end_ms_max": max((abs(v) for v in end_err), default=None)}


async def run_pipeline(cfg: RunConfig, stop: Optional[asyncio.Event] = None) -> dict:
    """Run one configuration and return its result dict (what ``summary.json`` holds per run)."""
    if (cfg.wav is None) == (cfg.capture is None):
        raise ValueError("give exactly one source: wav or capture")
    if cfg.speaker is not None and cfg.translator is None:
        raise ValueError("speech output needs translation")
    if cfg.capture is not None and not cfg.duration_s:
        raise ValueError("live capture needs a duration")
    loop = asyncio.get_running_loop()
    stop = stop or asyncio.Event()
    recorder = m.LatencyRecorder()
    clock = m.StreamClock()
    translator = _resolve(cfg.translator)(cfg.secrets) if cfg.translator is not None else None
    speaker = _resolve(cfg.speaker)(cfg.secrets) if cfg.speaker is not None else None
    tracker = SegmentTracker(cfg, recorder, clock, translator, speaker)

    status_lock = threading.Lock()
    last_state: list[Optional[c.SttState]] = [None]

    def on_event(ev: Any) -> None:
        arrived = time.monotonic_ns()
        if isinstance(ev, c.SttStatus):
            # The stream clock restarts here, in the caller's thread, so frames sent after a
            # reconnect map to the new session without waiting for the event loop.
            with status_lock:
                if ev.state is c.SttState.LIVE and last_state[0] not in (None, c.SttState.CONNECTING,
                                                                         c.SttState.LIVE):
                    clock.new_session()
                last_state[0] = ev.state
        loop.call_soon_threadsafe(tracker.handle, ev, arrived)

    stt: Optional[SttSession] = None
    if cfg.stt is not None:
        stt_cfg = c.SttConfig(commit_strategy=cfg.commit_strategy, language_code=cfg.language_code)
        stt = _resolve(cfg.stt)(stt_cfg, on_event, cfg.secrets)
        await stt.start()
    accounting = FrameAccounting(clock, stt.send_audio if stt else None, cfg.gap_tolerance_ms)
    sampler = ResourceSampler(cfg.sample_interval_s)
    sampler.start()
    started_wall = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
    t_start = time.monotonic()
    replay: Optional[PacedReplay] = None
    backend = None
    source_report: dict = {}
    wav_s = 0.0
    interrupted = False
    try:
        if cfg.wav is not None:
            pcm = read_engine_wav(cfg.wav)
            wav_s = len(pcm) / c.ENGINE_SAMPLE_WIDTH / c.ENGINE_SAMPLE_RATE
            replay = PacedReplay(pcm, accounting, chunk_ms=cfg.chunk_ms, duration_s=cfg.duration_s,
                                 loop=cfg.loop)
            replay.start()
            while not replay.done and not stop.is_set():
                await asyncio.sleep(0.05)
            replay.stop()
            replay.join(2)
            source_report = replay.report()
        else:
            backend = _resolve(cfg.capture)()
            ok, reason = backend.available()
            if not ok:
                raise c.CaptureError(f"backend {backend.name} unavailable: {reason}")
            sources = backend.list_sources()
            chosen = next((s for s in sources if cfg.capture_source in (None, s.source_id)), None)
            if chosen is None:
                raise c.CaptureError(f"source {cfg.capture_source!r} not offered by {backend.name}")
            backend.start(chosen, accounting)
            deadline = time.monotonic() + cfg.duration_s
            while time.monotonic() < deadline and not stop.is_set():
                await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            await loop.run_in_executor(None, backend.stop)
            source_report = {"source": "capture", "backend": backend.name, "source_id": chosen.source_id,
                             "label": chosen.label}
        interrupted = stop.is_set()
        drain_deadline = time.monotonic() + cfg.drain_s
        while time.monotonic() < drain_deadline and not tracker.idle():
            await asyncio.sleep(0.02)
    finally:
        if backend is not None and replay is None:
            with contextlib.suppress(Exception):
                backend.stop()
        if stt is not None:
            await stt.close()
        await tracker.close()
        if speaker is not None:
            await speaker.close()
        sampler.stop()
    wall_s = time.monotonic() - t_start
    segments = recorder.snapshot()
    stt_stats = stt.stats() if stt is not None and hasattr(stt, "stats") else {}
    capture_report = accounting.report()
    dropped = capture_report["gaps"] + capture_report["dropped_chunks_at_stt_send"] + int(
        stt_stats.get("dropped_chunks", 0) or 0)
    result = {
        "config": cfg.describe(),
        "started": started_wall,
        "wall_s": wall_s,
        "interrupted": interrupted,
        "source": source_report,
        "capture": capture_report,
        "stability": {
            "reconnects": tracker.counts["reconnects"],
            "reconnects_reported_by_stt": stt_stats.get("reconnects"),
            "dropped_chunks": dropped,
            "gaps": capture_report["gaps"],
            "resources": sampler.report(),
        },
        "stt_events": tracker.counts,
        "stt_states": tracker.states,
        "stt_stats": stt_stats,
        "stream_sessions": clock.sessions,
        "end_stage": end_stage_for(cfg),
        "latency": recorder.summary(end_stage=end_stage_for(cfg)),
        "segments": [{"key": k, "stages_ns": s, **meta} for k, s, meta in segments],
        "samples": sampler.samples,
    }
    if cfg.manifest is not None and replay is not None:
        result["attribution_error"] = attribution_error(
            segments, load_manifest(cfg.manifest), replay.t0_ns, wav_s, replay.samples / c.ENGINE_SAMPLE_RATE)
    if cfg.gate:
        need = G1_MIN_SECONDS if cfg.gate.upper() == "G1" else 0.0
        result["gate"] = {"name": cfg.gate, "min_audio_s": need,
                          "audio_s": capture_report["audio_s"],
                          "duration_met": capture_report["audio_s"] >= need and not interrupted}
    return result


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def _f(v: Any, nd: int = 1) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def render_markdown(runs: list[dict], title: str) -> str:
    lines = [f"# {title}", ""]
    if len(runs) > 1:
        lines += ["## Comparison (ms)", "",
                  "| run | segments | end stage | e2e p50 | e2e p90 | e2e p95 | e2e max | speech end to commit p50 | p95 | reconnects | dropped | cpu mean % | rss max MB |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in runs:
            e = r["latency"]["intervals"]["end_to_end"]
            s = r["latency"]["intervals"]["speech_end_to_committed"]
            res = r["stability"]["resources"]
            lines.append("| " + " | ".join([
                r["config"]["label"], str(r["latency"]["segments"]), r["end_stage"],
                _f(e.get("p50")), _f(e.get("p90")), _f(e.get("p95")), _f(e.get("max")),
                _f(s.get("p50")), _f(s.get("p95")), str(r["stability"]["reconnects"]),
                str(r["stability"]["dropped_chunks"]), _f(res["cpu_self_pct"].get("mean")),
                _f(res["rss_self_mb"].get("max"))]) + " |")
        lines.append("")
    for r in runs:
        cfg = r["config"]
        lines += [f"## Run `{cfg['label']}`", "",
                  f"Source {cfg['source']}, STT `{cfg['stt']}`, commit strategy {cfg['commit_strategy']}, "
                  f"translator `{cfg['translator']}`, speaker `{cfg['speaker']}`, chunk {cfg['chunk_ms']} ms. "
                  f"Wall time {r['wall_s']:.1f} s, audio {r['capture']['audio_s']:.1f} s"
                  + (", interrupted." if r["interrupted"] else "."), ""]
        lat = r["latency"]
        lines += [f"Segments {lat['segments']}, unattributed {lat['unattributed']}. End to end runs from "
                  f"capture to {r['end_stage']}.", "",
                  "| interval | count | p50 | p90 | p95 | max | negative |", "|---|---|---|---|---|---|---|"]
        for name, d in lat["intervals"].items():
            lines.append(f"| {name} | {d['count']} | {_f(d.get('p50'))} | {_f(d.get('p90'))} | "
                         f"{_f(d.get('p95'))} | {_f(d.get('max'))} | {d['negative']} |")
        lines.append("")
        src = r["source"]
        if src.get("source") == "wav":
            la = src["lateness_ms"]
            sk = src["sink_call_ms"]
            lines += [f"Pacing: thread policy {src['thread_policy']}, delivery lateness p50 {_f(la.get('p50'), 3)} ms, "
                      f"p99 {_f(la.get('p99'), 3)} ms, max {_f(la.get('max'), 3)} ms over {src['chunks']} chunks, "
                      f"{src['late_over_2ms']} later than 2 ms, {src['late_over_5ms']} later than 5 ms. "
                      f"Sink call p50 {_f(sk.get('p50'), 3)} ms, max {_f(sk.get('max'), 3)} ms.", ""]
        cap = r["capture"]
        st = r["stability"]
        res = st["resources"]
        lines += ["| stability | value |", "|---|---|",
                  f"| reconnects | {st['reconnects']} |",
                  f"| dropped chunks (gaps + send errors + client) | {st['dropped_chunks']} |",
                  f"| capture gaps | {cap['gaps']} ({_f(cap['gap_ms_total'])} ms total) |",
                  f"| CPU self % mean / p95 / max | {_f(res['cpu_self_pct'].get('mean'))} / "
                  f"{_f(res['cpu_self_pct'].get('p95'))} / {_f(res['cpu_self_pct'].get('max'))} |",
                  f"| CPU tree % mean / max | {_f(res['cpu_tree_pct'].get('mean'))} / {_f(res['cpu_tree_pct'].get('max'))} |",
                  f"| RSS self MB min / max / growth | {_f(res['rss_self_mb'].get('min'))} / "
                  f"{_f(res['rss_self_mb'].get('max'))} / {_f(res['rss_self_growth_mb'])} |",
                  f"| resource samples | {res['count']} every {res['interval_s']} s |", ""]
        if "attribution_error" in r:
            ae = r["attribution_error"]
            lines += [f"Attribution error against the manifest: max abs start {_f(ae.get('abs_start_ms_max'), 3)} ms, "
                      f"max abs end {_f(ae.get('abs_end_ms_max'), 3)} ms.", ""]
        if "gate" in r:
            g = r["gate"]
            lines += [f"Gate {g['name']}: audio {g['audio_s']:.1f} s of {g['min_audio_s']:.0f} s required, "
                      f"duration met: {g['duration_met']}.", ""]
    return "\n".join(lines)


def write_outputs(runs: list[dict], out_dir: Path, title: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps({"title": title, "runs": runs}, indent=1, default=str))
    (out_dir / "summary.md").write_text(render_markdown(runs, title))
    return out_dir


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------

def gen_corpus(path: Path, *, seed: int, duration_s: float, burst_ms: tuple[int, int],
               gap_ms: tuple[int, int], freq_hz: tuple[float, float], grid_ms: int = 20,
               amplitude: float = 0.3, fade_ms: int = 5) -> dict:
    """Write a WAV of tone bursts separated by silence plus ``<path>.json`` with the burst times.

    Every boundary falls on a ``grid_ms`` grid, so with matching chunk size a
    burst starts at a chunk boundary. Output is identical for equal arguments.
    """
    rng = random.Random(seed)
    sr = c.ENGINE_SAMPLE_RATE
    grid = sr * grid_ms // 1000
    total = int(duration_s * sr) // grid * grid
    pcm = array("h", bytes(total * 2))
    fade = sr * fade_ms // 1000
    peak = int(32767 * amplitude)
    segments = []
    tail = max(grid, (gap_ms[0] * sr // 1000) // grid * grid)
    pos = (rng.randint(gap_ms[0], gap_ms[1]) * sr // 1000) // grid * grid
    while True:
        n = max(grid, (rng.randint(burst_ms[0], burst_ms[1]) * sr // 1000) // grid * grid)
        if pos + n + tail > total:
            break
        f = round(rng.uniform(freq_hz[0], freq_hz[1]), 1)
        w = 2 * math.pi * f / sr
        for i in range(n):
            env = 1.0
            if i < fade:
                env = 0.5 - 0.5 * math.cos(math.pi * i / fade)
            elif i >= n - fade:
                env = 0.5 - 0.5 * math.cos(math.pi * (n - 1 - i) / fade)
            pcm[pos + i] = int(peak * env * math.sin(w * i))
        segments.append({"start_s": pos / sr, "end_s": (pos + n) / sr, "freq_hz": f})
        pos += n + (rng.randint(gap_ms[0], gap_ms[1]) * sr // 1000) // grid * grid
    if sys.byteorder != "little":
        pcm.byteswap()
    raw = pcm.tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(c.ENGINE_CHANNELS)
        w.setsampwidth(c.ENGINE_SAMPLE_WIDTH)
        w.setframerate(sr)
        w.writeframes(raw)
    manifest = {
        "sample_rate": sr, "duration_s": total / sr, "seed": seed, "grid_ms": grid_ms,
        "params": {"burst_ms": list(burst_ms), "gap_ms": list(gap_ms), "freq_hz": list(freq_hz),
                   "amplitude": amplitude, "fade_ms": fade_ms},
        "pcm_sha256": hashlib.sha256(raw).hexdigest(),
        "segments": segments,
    }
    path.with_suffix(".json").write_text(json.dumps(manifest, indent=1))
    return manifest


# --------------------------------------------------------------------------
# Overhead benchmark
# --------------------------------------------------------------------------

def bench(n: int = 200_000, threads: int = 4) -> dict:
    """Cost per call of the recording paths, in ns, best of three passes."""
    def best(fn) -> float:
        times = []
        for _ in range(3):
            t = time.perf_counter_ns()
            fn()
            times.append((time.perf_counter_ns() - t) / n)
        return min(times)

    stages = m.STAGES

    def marks():
        r = m.LatencyRecorder(max_segments=n)
        mark = r.mark
        for i in range(n):
            mark(i >> 3, stages[i & 7])

    def marks_ts():
        r = m.LatencyRecorder(max_segments=n)
        mark = r.mark
        for i in range(n):
            mark(i >> 3, stages[i & 7], i)

    def frames():
        clk = m.StreamClock()
        on = clk.on_frame
        for i in range(n):
            on(640, i)

    def sink():
        acc = FrameAccounting(m.StreamClock(), lambda pcm, ts: None, 2.0)
        chunk = bytes(640)
        for i in range(n):
            acc(chunk, i * 20_000_000)

    def lookups():
        clk = m.StreamClock()
        for i in range(30000):
            clk.on_frame(640, i * 20_000_000)
        f = clk.to_capture_ns
        for i in range(n):
            f((i % 30000) * 0.02)

    def contended() -> float:
        r = m.LatencyRecorder(max_segments=n * threads)
        per = n // threads
        barrier = threading.Barrier(threads + 1)

        def work(base):
            barrier.wait()
            mark = r.mark
            for i in range(per):
                mark(base + (i >> 3), stages[i & 7])

        ts = [threading.Thread(target=work, args=(k * per,)) for k in range(threads)]
        for t in ts:
            t.start()
        barrier.wait()
        t0 = time.perf_counter_ns()
        for t in ts:
            t.join()
        return (time.perf_counter_ns() - t0) / (per * threads)

    def bound_marker():
        r = m.LatencyRecorder(max_segments=n)
        with m.bind_segment(r, 1):
            mk = m.mark_current
            for i in range(n):
                mk(stages[i & 7])

    return {
        "python": sys.version.split()[0],
        "n": n,
        "recorder_mark_ns": round(best(marks), 1),
        "recorder_mark_with_ts_ns": round(best(marks_ts), 1),
        "mark_current_bound_ns": round(best(bound_marker), 1),
        "stream_clock_on_frame_ns": round(best(frames), 1),
        "frame_accounting_call_ns": round(best(sink), 1),
        "stream_clock_lookup_ns": round(best(lookups), 1),
        f"recorder_mark_{threads}_threads_wall_ns_per_event": round(min(contended() for _ in range(3)), 1),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

EPILOG = """\
Factory specs are module:attribute, imported from the repo root:
  --capture    factory() -> CaptureBackend
  --stt        factory(config: SttConfig, on_event, secrets) -> session with
               async start(), send_audio(pcm, capture_ns), async close(), optional stats()
  --translator factory(secrets) -> Translator
  --speaker    factory(secrets) -> Speaker
Secrets: the harness reads no credentials from environment variables. Runs with
fake or keyless factories need no key; every factory gets a getter that returns
None. Real ElevenLabs runs take --key-file PATH: a file you own, mode 600,
outside the repository, holding only the key. The getter then returns it for
ELEVENLABS_API_KEY and None for every other name.

Speech output stages: a Speaker reports first audio and playback start with
hermeneus_engine.metrics.mark_current(Stage.TTS_FIRST_AUDIO) inside speak(), or
with a callable from stage_marker(Stage.PLAYBACK_START) taken inside speak() and
called from its audio thread. Without those calls the two intervals stay empty.

Examples:
  latency_harness.py gen-corpus --out corpus/bursts.wav --seed 7 --duration 60
  latency_harness.py run --wav corpus/bursts.wav --stt none --label pacing
  latency_harness.py run --wav talk.wav --loop --duration 600 --gate G1 \\
      --stt pkg.adapters:scribe --commit-strategy vad,turn_prediction \\
      --translator pkg.adapters:translator --translate on,off
  latency_harness.py run --capture pkg.a:make --capture pkg.b:make --duration 600 --stt pkg.adapters:scribe
  latency_harness.py bench
"""


def _matrix(args) -> list[RunConfig]:
    strategies = [c.CommitStrategy(s) for s in args.commit_strategy.split(",")]
    translate = [v == "on" for v in args.translate.split(",")]
    tts = [v == "on" for v in args.tts.split(",")]
    captures = args.capture or [None]
    stt = None if args.stt == "none" else args.stt
    if stt is None:
        strategies, translate, tts = strategies[:1], [False], [False]
    if any(translate) and not args.translator:
        raise SystemExit("--translate on needs --translator")
    if any(tts) and not args.speaker:
        raise SystemExit("--tts on needs --speaker")
    runs = []
    for cap, strat, tr, sp in itertools.product(captures, strategies, translate, tts):
        if sp and not tr:
            continue
        parts = [args.label]
        if len(captures) > 1:
            parts.append(cap.rsplit(":", 1)[0].rsplit(".", 1)[-1] + "." + cap.rsplit(":", 1)[1])
        if stt is not None:
            parts.append(strat.value)
            parts.append("tr" if tr else "notr")
            parts.append("tts" if sp else "notts")
        runs.append(RunConfig(
            label="-".join(parts), wav=Path(args.wav).expanduser() if args.wav else None,
            manifest=Path(args.manifest).expanduser() if args.manifest else None,
            capture=cap, capture_source=args.capture_source, stt=stt, commit_strategy=strat,
            language_code=args.language_code, translator=args.translator if tr else None,
            speaker=args.speaker if sp else None, source_language=args.language_code,
            target_language=args.target_language, chunk_ms=args.chunk_ms, duration_s=args.duration,
            loop=args.loop, drain_s=args.drain, sample_interval_s=args.sample_interval,
            gap_tolerance_ms=args.gap_tolerance_ms, gate=args.gate))
    return runs


async def _run_all(runs: list[RunConfig]) -> list[dict]:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGINT, stop.set)
    results = []
    for cfg in runs:
        print(f"run {cfg.label} ...", file=sys.stderr, flush=True)
        results.append(await run_pipeline(cfg, stop))
        if stop.is_set():
            break
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="latency_harness.py", description=__doc__.split("\n\n")[1],
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one configuration or a matrix of them",
                       epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--wav", help="WAV in engine format (16 kHz, 16 bit, mono) replayed at real time")
    src.add_argument("--capture", action="append", help="CaptureBackend factory spec; repeat to compare backends")
    r.add_argument("--capture-source", help="source_id to capture from (default: first listed)")
    r.add_argument("--manifest", help="ground truth JSON with segments[].start_s/end_s, for attribution error")
    r.add_argument("--loop", action="store_true", help="repeat the WAV until --duration is reached")
    r.add_argument("--duration", type=float, help="seconds of audio to send (required for --capture)")
    r.add_argument("--chunk-ms", type=int, default=20, help="replay chunk size (default 20)")
    r.add_argument("--stt", default="none", help="SttSession factory spec, or none for audio only")
    r.add_argument("--commit-strategy", default="vad", help="vad, turn_prediction or both comma separated")
    r.add_argument("--language-code", help="source language passed to STT and translator")
    r.add_argument("--translator", help="Translator factory spec")
    r.add_argument("--translate", default="on", help="on, off or on,off (needs --translator for on)")
    r.add_argument("--speaker", help="Speaker factory spec")
    r.add_argument("--tts", default="off", help="on, off or on,off (needs --speaker for on)")
    r.add_argument("--target-language", default="en")
    r.add_argument("--drain", type=float, default=5.0, help="seconds to wait for the last segments (default 5)")
    r.add_argument("--sample-interval", type=float, default=1.0, help="CPU and RSS sample period in s")
    r.add_argument("--gap-tolerance-ms", type=float, default=2.0, help="capture timestamp slack before a gap counts")
    r.add_argument("--gate", help="G1 requires at least 600 s of audio")
    r.add_argument("--key-file", help="file holding only the ElevenLabs key (mode 600, outside the repo)")
    r.add_argument("--label", default="run")
    r.add_argument("--out", help=f"output directory (default {DEFAULT_OUT}/<time>-<label>)")

    g = sub.add_parser("gen-corpus", help="write a deterministic tone burst WAV and its manifest")
    g.add_argument("--out", required=True)
    g.add_argument("--seed", type=int, default=1)
    g.add_argument("--duration", type=float, default=60.0)
    g.add_argument("--burst-ms", default="300-1500", help="min-max burst length")
    g.add_argument("--gap-ms", default="300-1500", help="min-max silence length")
    g.add_argument("--freq-hz", default="200-1000", help="min-max tone frequency")
    g.add_argument("--grid-ms", type=int, default=20)

    b = sub.add_parser("bench", help="measure recording overhead per event")
    b.add_argument("-n", type=int, default=200_000)

    args = p.parse_args(argv)
    if args.cmd == "gen-corpus":
        def rng(s, conv=int):
            lo, hi = s.split("-")
            return conv(lo), conv(hi)

        man = gen_corpus(Path(args.out).expanduser(), seed=args.seed, duration_s=args.duration,
                         burst_ms=rng(args.burst_ms), gap_ms=rng(args.gap_ms),
                         freq_hz=rng(args.freq_hz, float), grid_ms=args.grid_ms)
        print(json.dumps({"wav": args.out, "segments": len(man["segments"]),
                          "duration_s": man["duration_s"], "pcm_sha256": man["pcm_sha256"]}))
        return 0
    if args.cmd == "bench":
        print(json.dumps(bench(args.n), indent=1))
        return 0
    if args.capture and not args.duration:
        raise SystemExit("--capture needs --duration")
    if args.gate and args.gate.upper() == "G1" and (args.duration or 0) < G1_MIN_SECONDS:
        raise SystemExit(f"--gate G1 needs --duration {G1_MIN_SECONDS:.0f} or more")
    runs = _matrix(args)
    if args.key_file:
        getter = KeyFileSecrets(args.key_file)
        for cfg in runs:
            cfg.secrets = getter
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out).expanduser() if args.out else DEFAULT_OUT / f"{stamp}-{args.label}"
    results = asyncio.run(_run_all(runs))
    write_outputs(results, out, f"Latency harness: {args.label}")
    print(out / "summary.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
