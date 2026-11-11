"""The session transport on Windows: loopback TCP with a per-session token.

Why not the Unix socket: "loop.create_unix_connection() and
loop.create_unix_server() are not supported" by any event loop on Windows
(docs.python.org/3/library/asyncio-platforms.html, Windows).

Why not a named pipe: asyncio serves pipes only on ProactorEventLoop, and the
Hermes dashboard, where the pane's API runs, drives uvicorn on a
SelectorEventLoop on Windows (hermes_cli/web_server.py ``_windows_serve_loop_factory``),
which "does not support pipes" (same asyncio page). TCP works on both loops.

How it is protected: the server listens on 127.0.0.1 only, on a port the OS
picks, and writes ``{"transport", "host", "port", "token", "pid"}`` to the
endpoint file (the path the Unix socket uses on macOS, inside the session's
run directory under HERMES_HOME). Every request must carry the 256-bit token;
the server compares it with ``hmac.compare_digest`` and answers anything else
with an error. Only a process that can read the endpoint file can talk to the
session. The file is written to a temporary name and moved into place, so a
reader never sees half of it.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable

HOST = "127.0.0.1"


def new_token() -> str:
    return secrets.token_hex(32)


def token_ok(expected: str, given: Any) -> bool:
    return isinstance(given, str) and hmac.compare_digest(expected.encode(), given.encode())


def write_endpoint(path: Path, port: int, token: str) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    data = json.dumps({"transport": "tcp", "host": HOST, "port": int(port), "token": token, "pid": os.getpid()})
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def read_endpoint(path: Path) -> dict:
    """Raises OSError when there is no usable endpoint file."""
    try:
        info = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise OSError(f"unreadable session endpoint: {exc}") from None
    if not (isinstance(info, dict) and info.get("transport") == "tcp" and isinstance(info.get("port"), int)
            and isinstance(info.get("token"), str)):
        raise OSError("unreadable session endpoint")
    return info


async def start_server(handle: Callable, path: Path, limit: int) -> tuple[asyncio.AbstractServer, str]:
    token = new_token()
    server = await asyncio.start_server(handle, host=HOST, port=0, limit=limit)
    port = server.sockets[0].getsockname()[1]
    try:
        write_endpoint(path, port, token)
    except BaseException:
        server.close()
        raise
    return server, token


async def open_connection(path: Path, limit: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, str]:
    info = read_endpoint(path)
    reader, writer = await asyncio.open_connection(HOST, info["port"], limit=limit)
    return reader, writer, info["token"]
