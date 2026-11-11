"""Fakes for every session component and for the Hermes ctx. No network, no audio device."""

from __future__ import annotations

import asyncio
import dataclasses
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from hermeneus_engine.contracts import CaptureSource, LineState, SubtitleLine, SttState, SttStatus
from hermeneus_engine.session import SessionComponents, SessionOptions

SOURCE = CaptureSource(backend="virtual-device", source_id="BlackHole 2ch", label="BlackHole 2ch", detail="2 in")
SOURCE2 = CaptureSource(backend="process-tap", source_id="system", label="All system audio except Hermeneus")


def short_tmpdir() -> Path:
    """A data dir short enough for a macOS socket path (pytest's tmp_path often is not)."""
    return Path(tempfile.mkdtemp(prefix="hmn-", dir="/tmp"))


def remove_tmpdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


class FakeCapture:
    name = "virtual-device"

    def __init__(self, *, fail_start: Optional[str] = None, stop_delay: float = 0.0) -> None:
        self.sink: Optional[Callable[[bytes, int], None]] = None
        self.started = False
        self.stopped = 0
        self.fail_start = fail_start
        self.stop_delay = stop_delay

    def available(self):
        return True, "fake"

    def list_sources(self):
        return [SOURCE]

    def start(self, source, sink):
        from hermeneus_engine.contracts import CaptureError
        if self.fail_start:
            raise CaptureError(self.fail_start)
        self.sink = sink
        self.started = True

    def stop(self):
        if self.stop_delay:
            threading.Event().wait(self.stop_delay)
        self.stopped += 1

    def push(self, pcm: bytes, ns: int = 1) -> None:
        """Deliver audio the way a capture thread would."""
        t = threading.Thread(target=self.sink, args=(pcm, ns))
        t.start()
        t.join()


class FakeStt:
    def __init__(self, *, live_on_start: bool = True, close_delay: float = 0.0) -> None:
        self.audio: list[tuple[bytes, int]] = []
        self._queue: Optional[asyncio.Queue] = None
        self.live_on_start = live_on_start
        self.closed = False
        self.close_delay = close_delay

    async def start(self) -> None:
        self._queue = asyncio.Queue()
        if self.live_on_start:
            self._queue.put_nowait(SttStatus(SttState.LIVE))

    def send_audio(self, pcm: bytes, captured_ns: int) -> None:
        self.audio.append((pcm, captured_ns))

    def emit(self, event: Any) -> None:
        self._queue.put_nowait(event)

    async def events(self):
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        self.closed = True
        if self._queue is not None:
            self._queue.put_nowait(None)


class FakeScheduler:
    """Translates by calling ``translate(text)``; an exception gives a FAILED line, like W04's scheduler."""

    def __init__(self, on_line, translate: Callable[[str], str]) -> None:
        self.on_line = on_line
        self.translate = translate
        self.submitted: list[SubtitleLine] = []
        self.relabels: list[tuple[str, str]] = []
        self.closed = False
        self._tasks: set = set()

    def submit(self, line: SubtitleLine) -> None:
        self.submitted.append(line)
        task = asyncio.get_running_loop().create_task(self._run(line))
        self._tasks.add(task)

    async def _run(self, line: SubtitleLine) -> None:
        await asyncio.sleep(0)
        try:
            text = self.translate(line.original)
        except Exception as exc:
            self.on_line(dataclasses.replace(line, state=LineState.FAILED, error=str(exc)))
        else:
            self.on_line(dataclasses.replace(line, state=LineState.TRANSLATED, translation=text))

    def relabel_speaker(self, a: str, b: str) -> None:
        self.relabels.append((a, b))

    @property
    def backlog(self) -> int:
        return sum(1 for t in self._tasks if not t.done())

    async def aclose(self) -> None:
        self.closed = True
        for t in self._tasks:
            t.cancel()


class FakeSpeaker:
    def __init__(self, voice_id: str = "", device: str = "", *, fail_start: Optional[Exception] = None,
                 fail_speak: Optional[Exception] = None) -> None:
        self.voice_id = voice_id
        self.device = device
        self.spoken: list[tuple[str, str]] = []
        self.started_with: Optional[str] = None
        self.closed = False
        self.fail_start = fail_start
        self.fail_speak = fail_speak
        self.last_error = ""
        self.unhealthy = ""

    async def start(self, language=None):
        if self.fail_start is not None:
            raise self.fail_start
        self.started_with = language

    async def speak(self, text, *, language):
        if self.fail_speak is not None:
            raise self.fail_speak
        self.spoken.append((text, language))

    def health(self) -> str:
        return self.unhealthy

    async def close(self):
        self.closed = True


@dataclasses.dataclass
class Rig:
    capture: FakeCapture
    stt: FakeStt
    speaker: Optional[FakeSpeaker] = None
    scheduler: Optional[FakeScheduler] = None
    translate: Callable[[str], str] = lambda text: f"<{text}>"
    capture_error: Optional[Callable] = None
    # Live changes: what the factories hand out, and what they were asked for.
    live_changes: bool = True
    next_capture: Optional[FakeCapture] = None
    next_speaker_error: Optional[Exception] = None
    speakers: list = dataclasses.field(default_factory=list)
    captures: list = dataclasses.field(default_factory=list)
    route_checks: list = dataclasses.field(default_factory=list)
    route_error: Optional[Exception] = None

    def build(self, options: SessionOptions, on_capture_error) -> SessionComponents:
        self.capture_error = on_capture_error

        def factory(on_line):
            self.scheduler = FakeScheduler(on_line, self.translate)
            return self.scheduler

        def speaker_factory(voice_id, device, source):
            speaker = FakeSpeaker(voice_id, device, fail_start=self.next_speaker_error)
            self.speakers.append(speaker)
            return speaker

        def capture_factory(source):
            capture = self.next_capture or FakeCapture()
            self.next_capture = None
            self.captures.append((source, capture))
            return capture

        def route_check(device, source):
            self.route_checks.append((device, source))
            if self.route_error is not None:
                raise self.route_error

        if not self.live_changes:
            return SessionComponents(capture=self.capture, stt=self.stt, scheduler_factory=factory,
                                     speaker=self.speaker)
        return SessionComponents(capture=self.capture, stt=self.stt, scheduler_factory=factory,
                                 speaker=self.speaker, speaker_factory=speaker_factory,
                                 capture_factory=capture_factory, route_check=route_check)


def make_rig(**kw) -> Rig:
    return Rig(capture=kw.pop("capture", FakeCapture()), stt=kw.pop("stt", FakeStt()), **kw)


def options(**kw) -> SessionOptions:
    return SessionOptions(source=kw.pop("source", SOURCE), target_language=kw.pop("target_language", "tr"), **kw)


async def wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


class FakeCtx:
    """Records what register() registers, like hermes_cli.plugin_validate's probe does."""

    def __init__(self, settings: Optional[dict] = None) -> None:
        self.aux_tasks: list[tuple[str, dict]] = []
        self.commands: dict[str, dict] = {}
        self.cli_commands: dict[str, dict] = {}
        self.unload_callbacks: list[Callable] = []
        self.tools: list = []
        self.hooks: list = []
        self._settings = settings or {}
        self.llm = FakeLlm()

    def register_auxiliary_task(self, key, *, display_name, description, defaults=None, inherit_from=None):
        self.aux_tasks.append((key, {"display_name": display_name, "description": description}))

    def register_command(self, name, handler, description="", args_hint="", argument_mode=None):
        self.commands[name] = {"handler": handler, "description": description, "args_hint": args_hint}

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands[name] = {"help": help, "setup_fn": setup_fn, "handler_fn": handler_fn}

    def register_tool(self, *a, **k):
        self.tools.append(a)

    def register_hook(self, *a, **k):
        self.hooks.append(a)

    def on_unload(self, callback):
        self.unload_callbacks.append(callback)

    def get_config(self, key, default=None):
        return self._settings.get(key, default)


class FakeLlm:
    def __init__(self, text: str = "merhaba") -> None:
        self.calls: list[dict] = []
        self.text = text

    async def acomplete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return type("Result", (), {"text": self.text})()
