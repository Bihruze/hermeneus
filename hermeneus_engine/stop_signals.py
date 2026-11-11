"""Ctrl+C and termination signals for ``hermes hermeneus run``, on every platform.

``loop.add_signal_handler()`` is "not supported" by any event loop on Windows
(docs.python.org/3/library/asyncio-platforms.html). There the handler goes
through ``signal.signal`` for SIGINT (Ctrl+C) and SIGBREAK (Ctrl+Break, and the
console close event CPython maps to it) and hands the stop to the loop with
``call_soon_threadsafe``. The previous handlers come back on uninstall.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Callable


def install(loop: asyncio.AbstractEventLoop, on_stop: Callable[[], None]) -> Callable[[], None]:
    """Route stop signals to ``on_stop`` on ``loop``. Returns the function that undoes it."""
    if sys.platform != "win32":
        sigs = (signal.SIGINT, signal.SIGTERM)
        for sig in sigs:
            loop.add_signal_handler(sig, on_stop)

        def remove() -> None:
            for sig in sigs:
                loop.remove_signal_handler(sig)
        return remove

    previous: dict = {}

    def handler(_signum, _frame) -> None:
        try:
            loop.call_soon_threadsafe(on_stop)
        except RuntimeError:
            pass  # the loop already closed

    for name in ("SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            previous[sig] = signal.signal(sig, handler)
        except ValueError:  # not the main thread; Hermes keeps its own handler
            pass

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except (ValueError, TypeError):
                pass
    return restore
