"""The session's Unix domain socket: JSON lines, one request per connection.

The owning process serves its bus at ``<run dir>/sock`` (mode 0600 inside a
0700 directory). There is no TCP port. A client writes one request line and
reads JSON lines back:

``{"op": "status"}``
    one ``status`` event, then the server closes.
``{"op": "lines", "after": <seq or null>}``
    one ``{"type": "lines", "seq": <latest>, "reset": bool, "events": [...]}``.
``{"op": "subscribe", "after": <seq or null>}``
    a ``hello`` event, then (with ``after``) the events since ``after``, then
    live events until the session ends. A client that falls too far behind
    is disconnected and should subscribe again with its last seq.
``{"op": "stop"}``
    ``{"type": "ok"}``, then the session stops.
``{"op": "overlay", "on": <true, false or null>, "port": <int or null>}``
    switches the 127.0.0.1 overlay page (null only asks) and answers
    ``{"type": "overlay", "running", "url", "port", "error"}``.

Event shapes are documented in ``docs/events.md``.

On Windows the same requests travel over loopback TCP, and every request
carries the session token from the endpoint file (``_windows/ipc.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Optional

from .bus import SubtitleBus

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 4096
REQUEST_TIMEOUT_S = 2.0
CONNECT_TIMEOUT_S = 1.0
STREAM_LIMIT = 1 << 20


def _use_tcp() -> bool:
    """Windows has no asyncio Unix sockets; it uses the token-guarded loopback transport."""
    return sys.platform == "win32"


class SessionUnavailable(ConnectionError):
    """No session answers on the socket."""


def _encode(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class BusServer:
    def __init__(self, path: Path, bus: SubtitleBus, *, on_stop: Callable[[], None],
                 on_overlay: Optional[Callable[[Optional[bool], Optional[int]], Awaitable[dict]]] = None) -> None:
        self.path = Path(path)
        self._bus = bus
        self._on_stop = on_stop
        self._on_overlay = on_overlay
        self._server: Optional[asyncio.AbstractServer] = None
        self._clients: set[asyncio.Task] = set()
        self._token: Optional[str] = None

    async def start(self) -> None:
        if _use_tcp():
            from ._windows.ipc import start_server
            self._server, self._token = await start_server(self._handle, self.path, STREAM_LIMIT)
            return
        self._server = await asyncio.start_unix_server(  # type: ignore[attr-defined]  # POSIX branch
            self._handle, path=str(self.path), limit=STREAM_LIMIT)
        # The run directory is 0700, so nobody else can reach the socket in
        # the moment before this chmod.
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            for task in list(self._clients):
                task.cancel()
            for task in list(self._clients):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 1.0)
            self._server = None
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
            except (asyncio.TimeoutError, ValueError):
                return
            if not raw or len(raw) > MAX_REQUEST_BYTES:
                return
            try:
                request = json.loads(raw)
            except ValueError:
                request = None
            if not isinstance(request, dict):
                writer.write(_encode({"type": "error", "message": "bad request"}))
                await writer.drain()
                return
            if self._token is not None and not _token_matches(self._token, request.get("token")):
                writer.write(_encode({"type": "error", "message": "unauthorized"}))
                await writer.drain()
                return
            await self._serve(request, writer)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._clients.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _serve(self, request: dict, writer: asyncio.StreamWriter) -> None:
        op = request.get("op")
        if op == "status":
            writer.write(_encode(self._bus.status()))
        elif op == "lines":
            after = request.get("after")
            events, reset = self._bus.events_after(None if after is None else _as_seq(after))
            writer.write(_encode({"type": "lines", "seq": self._bus.last_seq, "reset": reset, "events": events}))
        elif op == "stop":
            writer.write(_encode({"type": "ok"}))
            await writer.drain()
            self._on_stop()
            return
        elif op == "subscribe":
            await self._stream(request, writer)
            return
        elif op == "overlay":
            on, port = request.get("on"), request.get("port")
            if self._on_overlay is None:
                writer.write(_encode({"type": "error", "message": "this session has no overlay page"}))
            elif on not in (True, False, None) or not (port is None or type(port) is int):
                writer.write(_encode({"type": "error", "message": "bad overlay request"}))
            else:
                writer.write(_encode(await self._on_overlay(on, port)))
        else:
            writer.write(_encode({"type": "error", "message": f"unknown op {op!r}"}))
        await writer.drain()

    async def _stream(self, request: dict, writer: asyncio.StreamWriter) -> None:
        sub = self._bus.subscribe()
        try:
            writer.write(_encode(self._bus.hello()))
            sent = self._bus.last_seq
            after = request.get("after")
            if after is not None:
                events, reset = self._bus.events_after(_as_seq(after))
                if reset:
                    writer.write(_encode({"type": "reset"}))
                for event in events:
                    writer.write(_encode(event))
                    sent = max(sent, event["seq"])
            await writer.drain()
            async for event in sub:
                if event["seq"] <= sent:
                    continue
                sent = event["seq"]
                writer.write(_encode(event))
                await writer.drain()
        finally:
            sub.close()


def _token_matches(expected: str, given) -> bool:
    from ._windows.ipc import token_ok
    return token_ok(expected, given)


def _as_seq(value) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


class SessionClient:
    """Talks to the session that owns ``path``. Every call raises SessionUnavailable when nobody answers."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._token: Optional[str] = None

    async def _open(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            if _use_tcp():
                from ._windows.ipc import open_connection
                reader, writer, self._token = await asyncio.wait_for(
                    open_connection(self.path, STREAM_LIMIT), CONNECT_TIMEOUT_S)
                return reader, writer
            return await asyncio.wait_for(
                asyncio.open_unix_connection(  # type: ignore[attr-defined]  # POSIX branch
                    str(self.path), limit=STREAM_LIMIT), CONNECT_TIMEOUT_S)
        except (FileNotFoundError, ConnectionRefusedError, asyncio.TimeoutError, OSError) as exc:
            raise SessionUnavailable(str(exc) or type(exc).__name__) from None

    async def _request(self, request: dict) -> dict:
        reader, writer = await self._open()
        try:
            writer.write(_encode(self._signed(request)))
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), REQUEST_TIMEOUT_S)
        except (ConnectionError, asyncio.TimeoutError) as exc:
            raise SessionUnavailable(str(exc) or type(exc).__name__) from None
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        if not raw:
            raise SessionUnavailable("the session closed the connection")
        return json.loads(raw)

    def _signed(self, request: dict) -> dict:
        return {**request, "token": self._token} if self._token is not None else request

    async def status(self) -> dict:
        return await self._request({"op": "status"})

    async def lines(self, after: Optional[int] = None) -> dict:
        return await self._request({"op": "lines", "after": after})

    async def stop(self) -> None:
        reply = await self._request({"op": "stop"})
        if reply.get("type") != "ok":
            raise SessionUnavailable(f"stop refused: {reply}")

    async def overlay(self, on: Optional[bool] = None, port: Optional[int] = None) -> dict:
        """Switch the overlay page (None only asks). Raises SessionUnavailable when the owner cannot."""
        reply = await self._request({"op": "overlay", "on": on, "port": port})
        if reply.get("type") != "overlay":
            raise SessionUnavailable(str(reply.get("message") or reply))
        return reply

    async def subscribe(self, after: Optional[int] = None) -> AsyncIterator[dict]:
        """Yield events until the session ends or the connection drops."""
        reader, writer = await self._open()
        try:
            writer.write(_encode(self._signed({"op": "subscribe", "after": after})))
            await writer.drain()
            while True:
                raw = await reader.readline()
                if not raw:
                    return
                yield json.loads(raw)
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
