"""In-memory subtitle bus.

The bus keeps the most recent subtitle lines of one session in memory and
publishes every change to its subscribers as an event dict (the schema in
``docs/events.md``). Nothing is written to disk.

Every event carries a ``seq`` that grows by one per event across all event
types, so a consumer that lost its connection asks for ``events_after(seq)``
and gets the current state of every line that changed since then.

The bus is thread safe. Publishers may call it from any thread; each
subscriber receives events on the event loop it subscribed from.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections import OrderedDict, deque
from dataclasses import replace
from typing import Any, Optional

from .contracts import SubtitleLine

EVENT_SCHEMA_VERSION = 1

DEFAULT_MAX_LINES = 200
DEFAULT_MAX_RELABELS = 256
DEFAULT_SUBSCRIBER_QUEUE = 1000

SESSION_STATES = ("idle", "starting", "live", "reconnecting", "stopping", "failed")

# One seq counter per process, shared by every bus this process creates, so a
# consumer of this process never sees seq go backwards across sessions.
_PROCESS_SEQ = itertools.count(1)
_PROCESS_SEQ_LOCK = threading.Lock()
_last_process_seq = 0


def next_process_seq() -> int:
    global _last_process_seq
    with _PROCESS_SEQ_LOCK:
        _last_process_seq = next(_PROCESS_SEQ)
        return _last_process_seq


def last_process_seq() -> int:
    with _PROCESS_SEQ_LOCK:
        return _last_process_seq


_LANGUAGES: Optional[list] = None


def languages_payload() -> list[dict]:
    """``status.languages``: every subtitle target, with whether spoken translation supports it."""
    global _LANGUAGES
    if _LANGUAGES is None:
        from . import languages as table
        _LANGUAGES = [{"code": lang.iso639_1 or lang.iso639_3, "name": lang.english_name,
                       "speakable": lang.tts_flash_v2_5_supported}
                      for lang in (table.get(code) for code in table.target_codes())]
    return [dict(entry) for entry in _LANGUAGES]


def wire_language(code: Optional[str]) -> Optional[str]:
    """ISO 639-1 where one exists, else ISO 639-3. Unknown codes pass through unchanged."""
    if not code:
        return code
    from . import languages as table
    try:
        lang = table.get(code)
    except table.UnknownLanguageError:
        return code
    return lang.iso639_1 or lang.iso639_3


def idle_status(message: str = "") -> dict[str, Any]:
    """Status body for a machine with no session."""
    return {"state": "idle", "message": message, "source": None, "target_language": "",
            "detected_language": None, "speak": False, "metrics": {}, "languages": languages_payload()}


class Subscription:
    """Events for one consumer. Iterate with ``async for``.

    Iteration ends when the bus closes or when this consumer fell more than
    ``maxsize`` events behind. In the second case ``overflowed`` is True and
    the consumer should resync with ``events_after``.
    """

    def __init__(self, bus: "SubtitleBus", loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self._bus = bus
        self._loop = loop
        self._queue: asyncio.Queue = asyncio.Queue()
        self._maxsize = maxsize
        self._closed = False
        self.overflowed = False

    # Runs on the subscriber's loop.
    def _put(self, event: Optional[dict]) -> None:
        if self._closed:
            return
        if event is None:
            self._closed = True
            self._queue.put_nowait(None)
            return
        if self._queue.qsize() >= self._maxsize:
            self.overflowed = True
            self._closed = True
            self._queue.put_nowait(None)
            self._bus._remove(self)
            return
        self._queue.put_nowait(event)

    def _deliver(self, event: Optional[dict]) -> bool:
        """Hand an event over from any thread. False when the loop is gone."""
        try:
            self._loop.call_soon_threadsafe(self._put, event)
            return True
        except RuntimeError:
            return False

    def __aiter__(self) -> "Subscription":
        return self

    async def __anext__(self) -> dict:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def get(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Next event, or None when the subscription ended or the timeout passed."""
        try:
            event = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if event is None:
            self._queue.put_nowait(None)
        return event

    def close(self) -> None:
        self._bus._remove(self)
        self._deliver(None)


class SubtitleBus:
    def __init__(self, *, max_lines: int = DEFAULT_MAX_LINES, max_relabels: int = DEFAULT_MAX_RELABELS) -> None:
        if max_lines < 1:
            raise ValueError("max_lines must be at least 1")
        self._lock = threading.Lock()
        self._max_lines = max_lines
        self._seq = last_process_seq()
        self._lines: "OrderedDict[str, tuple[int, SubtitleLine]]" = OrderedDict()
        self._relabels: deque = deque(maxlen=max_relabels)
        # Oldest seq whose effect a consumer can still be told about. A consumer
        # that last saw an older seq gets a reset.
        self._floor = self._seq
        self._status: dict[str, Any] = {"type": "status", "seq": self._seq, **idle_status()}
        self._subscribers: list[Subscription] = []
        self._closed = False

    # -- publishing -------------------------------------------------------

    def _next(self) -> int:
        self._seq = next_process_seq()
        return self._seq

    def _fan_out(self, event: dict) -> None:
        for sub in list(self._subscribers):
            if not sub._deliver(event):
                self._subscribers.remove(sub)

    def publish_line(self, line: SubtitleLine) -> dict:
        """Store the line (same ``line_id`` replaces it) and publish it."""
        with self._lock:
            seq = self._next()
            stored = replace(line, timings_ns=dict(line.timings_ns))
            self._lines.pop(line.line_id, None)
            self._lines[line.line_id] = (seq, stored)
            while len(self._lines) > self._max_lines:
                _old_id, (old_seq, _old) = self._lines.popitem(last=False)
                self._floor = max(self._floor, old_seq)
            event = {"type": "line", "seq": seq, "line": stored.to_dict()}
            self._fan_out(event)
            return event

    def publish_relabel(self, from_speaker: str, to_speaker: str) -> dict:
        """Rename a speaker in every stored line and publish the relabel."""
        with self._lock:
            seq = self._next()
            for line_id, (line_seq, line) in list(self._lines.items()):
                if line.speaker_id == from_speaker:
                    self._lines[line_id] = (line_seq, replace(line, speaker_id=to_speaker))
            if len(self._relabels) == self._relabels.maxlen:
                self._floor = max(self._floor, self._relabels[0]["seq"])
            event = {"type": "relabel", "seq": seq, "from": from_speaker, "to": to_speaker}
            self._relabels.append(event)
            self._fan_out(event)
            return event

    def publish_status(self, *, state: str, message: str = "", source: Optional[dict] = None,
                       target_language: str = "", speak: bool = False,
                       detected_language: Optional[str] = None, metrics: Optional[dict] = None) -> dict:
        if state not in SESSION_STATES:
            raise ValueError(f"unknown session state {state!r}")
        with self._lock:
            event = {"type": "status", "seq": self._next(), "state": state, "message": message,
                     "source": source, "target_language": target_language, "detected_language": detected_language,
                     "speak": bool(speak), "metrics": dict(metrics or {}), "languages": languages_payload()}
            self._status = event
            self._fan_out(event)
            return event

    # -- reading ----------------------------------------------------------

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq

    def status(self) -> dict:
        """The latest status event."""
        with self._lock:
            return dict(self._status)

    def hello(self) -> dict:
        with self._lock:
            session = {k: v for k, v in self._status.items() if k != "type"}
            session["seq"] = self._seq
            return {"type": "hello", "version": EVENT_SCHEMA_VERSION, "seq": self._seq, "session": session}

    def lines(self) -> list[SubtitleLine]:
        with self._lock:
            return [line for _seq, line in self._lines.values()]

    def events_after(self, after: Optional[int]) -> tuple[list[dict], bool]:
        """Events a consumer that last saw ``after`` needs, oldest first.

        Returns ``(events, reset)``. With ``reset`` True (no ``after``, or an
        ``after`` outside what the bus holds) the consumer must drop its lines
        and use the returned events, which then hold every stored line.
        """
        with self._lock:
            reset = after is None or after < self._floor or after > self._seq
            since = -1 if reset else after
            events: list[dict] = []
            for seq, line in self._lines.values():
                if seq > since:
                    events.append({"type": "line", "seq": seq, "line": line.to_dict()})
            if not reset:
                events.extend(dict(r) for r in self._relabels if r["seq"] > since)
            if self._status["seq"] > since:
                events.append(dict(self._status))
            events.sort(key=lambda e: e["seq"])
            return events, reset

    # -- subscribers ------------------------------------------------------

    def subscribe(self, *, maxsize: int = DEFAULT_SUBSCRIBER_QUEUE) -> Subscription:
        """Subscribe from the running event loop."""
        sub = Subscription(self, asyncio.get_running_loop(), maxsize)
        with self._lock:
            if self._closed:
                sub._deliver(None)
            else:
                self._subscribers.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def close(self) -> None:
        """End every subscription. Stored lines stay readable."""
        with self._lock:
            self._closed = True
            subs, self._subscribers = self._subscribers, []
        for sub in subs:
            sub._deliver(None)
