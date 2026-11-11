"""One door to the machine's session, whoever owns it.

Commands, the CLI and the dashboard API all go through a ``Controller``.
When this process owns the running session the controller reads its bus
directly; otherwise it talks to the owner over the Unix socket.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

from .bus import idle_status, last_process_seq
from . import messages
from .ipc import SessionClient, SessionUnavailable, SettingsRefused
from .runtime import ComponentBuilder, SessionBusy, SessionHost, lock_holder, socket_path
from .session import SessionOptions

STOP_WAIT_S = 3.0
IDLE_HEARTBEAT_S = 3.0
SESSION_POLL_S = 0.25


def _status_event(body: dict) -> dict:
    return {"type": "status", "seq": body.get("seq", 0), **{k: v for k, v in body.items() if k not in ("type", "seq")}}


class Controller:
    def __init__(self, data_dir: Callable[[], Path], *, owner: str) -> None:
        self._data_dir = data_dir
        self.host_owner = owner
        self._host: Optional[SessionHost] = None

    @property
    def data_dir(self) -> Path:
        return Path(self._data_dir())

    def _client(self) -> SessionClient:
        return SessionClient(socket_path(self.data_dir))

    @property
    def local_bus(self):
        host = self._host
        if host is not None and host.running and host.bus is not None:
            return host.bus
        return None

    async def status(self) -> dict:
        bus = self.local_bus
        if bus is not None:
            return bus.status()
        try:
            return await self._client().status()
        except SessionUnavailable:
            pass
        holder = lock_holder(self.data_dir)
        if holder:
            return _status_event({**idle_status(), "state": "starting",
                                  "message": f"a session is starting in process {holder.get('pid')}"})
        return _status_event(idle_status())

    async def start(self, options: SessionOptions, build: ComponentBuilder, *, overlay_page=None) -> dict:
        """Start a session owned by this process. Raises SessionBusy, UsageError-like errors or the start error."""
        if self.local_bus is not None:
            raise SessionBusy({"pid": None, "owner": self.host_owner})
        holder = lock_holder(self.data_dir)
        if holder:
            raise SessionBusy(holder)
        if self._host is None or not self._host.running:
            self._host = SessionHost(self.data_dir, owner=self.host_owner)
        host = self._host
        return await asyncio.to_thread(lambda: host.start(options, build, overlay_page=overlay_page))

    async def overlay(self, on: Optional[bool] = None, port: Optional[int] = None) -> Optional[dict]:
        """Switch the session's overlay page (None only asks). None when no session answers."""
        host = self._host
        runtime = host.runtime if host is not None and host.running else None
        if runtime is not None and runtime.overlay_page is not None:
            return await asyncio.to_thread(runtime.overlay_page.switch, on, port)
        try:
            return await self._client().overlay(on, port)
        except SessionUnavailable:
            return None

    async def stop(self) -> dict:
        host = self._host
        if host is not None and host.running:
            await asyncio.to_thread(host.stop)
            return await self.status()
        try:
            await self._client().stop()
        except SessionUnavailable:
            holder = lock_holder(self.data_dir)
            if holder:
                return _status_event({**idle_status(), "state": "starting",
                                      "message": f"the session in process {holder.get('pid')} is not answering yet"})
            return _status_event(idle_status("no session is running"))
        deadline = time.monotonic() + STOP_WAIT_S
        while time.monotonic() < deadline and lock_holder(self.data_dir):
            await asyncio.sleep(0.05)
        return await self.status()

    async def set(self, changes: dict) -> dict:
        """Change the running session's settings, from any process. Answers at once.

        Returns ``{"ok", "accepted", "applying", "status"}``; raises
        SettingsRefused with the reason. The owner's socket serves the change
        even when this process owns the session, so there is one code path.
        """
        try:
            return await self._client().set(changes)
        except SessionUnavailable:
            if lock_holder(self.data_dir):
                raise SettingsRefused("The session is still starting; try the change again in a moment.") from None
            raise SettingsRefused(messages.NOT_RUNNING) from None

    def stop_local(self, timeout: float = 2.0) -> bool:
        """Synchronous stop for unload hooks. True when nothing local is left running."""
        host = self._host
        if host is None:
            return True
        return host.stop(timeout)

    async def lines(self, after: Optional[int] = None) -> dict:
        """``{"seq", "reset", "events"}`` for a consumer that last saw ``after`` (None: everything)."""
        bus = self.local_bus
        if bus is not None:
            events, reset = bus.events_after(after)
            return {"seq": bus.last_seq, "reset": reset, "events": events}
        try:
            page = await self._client().lines(after)
            page.pop("type", None)
            return page
        except SessionUnavailable:
            status = self._idle_event(await self.status())
            return {"seq": status["seq"], "reset": after is None or after != status["seq"], "events": [status]}

    @staticmethod
    def _idle_event(status: dict) -> dict:
        """A status event for a machine without a reachable session. Its seq is this process's
        latest, so it never moves a consumer's seq backwards."""
        return {**status, "type": "status", "seq": max(int(status.get("seq") or 0), last_process_seq())}

    async def _session_events(self, after: Optional[int]) -> AsyncIterator[dict]:
        """hello, the events since ``after``, then live events, until that session ends."""
        bus = self.local_bus
        if bus is not None:
            sub = bus.subscribe()
            try:
                yield bus.hello()
                sent = bus.last_seq
                if after is not None:
                    events, reset = bus.events_after(after)
                    if reset:
                        yield {"type": "reset", "seq": sent}
                    for event in events:
                        sent = max(sent, event["seq"])
                        yield event
                async for event in sub:
                    if event["seq"] > sent:
                        sent = event["seq"]
                        yield event
            finally:
                sub.close()
            return
        async for event in self._client().subscribe(after):
            yield event

    async def subscribe(self, after: Optional[int] = None, *,
                        heartbeat_s: float = IDLE_HEARTBEAT_S) -> AsyncIterator[dict]:
        """Events for a long-lived consumer such as the pane's socket. Never ends by itself.

        With a session: its hello, missed events and live events. Without one:
        a hello, then a status heartbeat every ``heartbeat_s`` seconds, while
        watching for a session to appear (in this process or another).
        """
        while True:
            try:
                got_any = False
                async for event in self._session_events(after):
                    got_any = True
                    if isinstance(event.get("seq"), int):
                        after = event["seq"]
                    yield event
                if got_any:
                    continue  # that session ended; its final status was the last event
            except SessionUnavailable:
                pass
            status = self._idle_event(await self.status())
            yield {"type": "hello", "version": 1, "seq": status["seq"],
                   "session": {k: v for k, v in status.items() if k != "type"}}
            # Look for a new session often; send a heartbeat less often.
            waited = 0.0
            while not (self.local_bus is not None or lock_holder(self.data_dir)):
                step = min(SESSION_POLL_S, heartbeat_s)
                await asyncio.sleep(step)
                waited += step
                if waited >= heartbeat_s:
                    waited = 0.0
                    yield self._idle_event(await self.status())
            await asyncio.sleep(SESSION_POLL_S)  # let a starting owner open its socket
