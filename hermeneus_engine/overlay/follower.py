"""Follow the session's event stream for the overlay process.

The overlay lives as long as the session. When the stream ends and the
session lock is gone, the session has stopped and ``follow`` returns. When the
stream drops while the lock is still held (the owner is busy, or the socket
was briefly unavailable), it subscribes again from the last ``seq`` it saw,
with a growing pause, and gives up after ``give_up_s`` without a connection.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator, Callable, Optional

from ..ipc import SessionUnavailable

RETRY_FIRST_S = 0.25
RETRY_MAX_S = 2.0
GIVE_UP_S = 30.0

Subscribe = Callable[[Optional[int]], AsyncIterator[dict]]


async def follow(subscribe: Subscribe, on_event: Callable[[dict], None], session_alive: Callable[[], bool], *,
                 stop: Optional[asyncio.Event] = None, give_up_s: float = GIVE_UP_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep) -> str:
    """Feed events to ``on_event`` until the session ends. Returns why it ended:
    ``"ended"`` (the session stopped), ``"lost"`` (no connection for ``give_up_s``) or ``"stopped"``."""
    # 0 asks for every stored line first (docs/events.md: an uncovered after gives a reset and all lines).
    after = 0
    delay = RETRY_FIRST_S
    lost_since: Optional[float] = None
    while stop is None or not stop.is_set():
        connected = False
        try:
            async for event in subscribe(after):
                connected = True
                lost_since = None
                delay = RETRY_FIRST_S
                seq = event.get("seq")
                if isinstance(seq, int) and event.get("type") != "hello":
                    after = max(after, seq)
                on_event(event)
                if stop is not None and stop.is_set():
                    return "stopped"
        except SessionUnavailable:
            pass
        except (ConnectionError, OSError, ValueError):
            pass
        if not session_alive():
            return "ended"
        now = clock()
        if not connected:
            lost_since = now if lost_since is None else lost_since
            if now - lost_since >= give_up_s:
                return "lost"
        await sleep(delay)
        delay = min(delay * 2, RETRY_MAX_S)
    return "stopped"
