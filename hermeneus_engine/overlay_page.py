"""The overlay page: live subtitles on ``http://127.0.0.1:<port>/<token>/`` for OBS and second screens.

The page is off by default. When the ``overlay_page`` setting is on, the
process that owns the session serves it for as long as the session runs
(``hermes hermeneus overlay-page on|off`` switches it, also during a session).

What the server does:

* It binds 127.0.0.1 only. A port another program already listens on is an
  error with the fix in the message; no other port is picked.
* Every request needs the token in the path. The token comes from
  ``secrets.token_urlsafe`` and is new each time the server starts.
* The ``Host`` header must be ``127.0.0.1:<port>`` or ``localhost:<port>``,
  which blocks DNS rebinding.
* It answers GET and HEAD only, sends no CORS headers and has no control
  routes. ``/<token>/`` is the page (``overlay_page.html``), ``/<token>/events``
  a Server-Sent Events stream of subtitle events.

Events come from the session's Unix socket through ``Controller.subscribe``,
the same stream the pane relays, so the page never opens a second speech to
text session. The server runs on its own thread and event loop, away from the
session's loop.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import errno
import hashlib
import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Mapping, Optional
from urllib.parse import parse_qs, urlsplit

from . import overlay_settings

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
DEFAULT_PORT = 47821
PORT_MIN, PORT_MAX = 1024, 65535
KEY_ENABLED = "overlay_page"
KEY_PORT = "overlay_page_port"

PAGE_FILE = Path(__file__).with_name("overlay_page.html")
SETTINGS_PLACEHOLDER = "{}/*HERMENEUS_SETTINGS*/"
THEME_PLACEHOLDER = "/*HERMENEUS_THEME*/"

MAX_CLIENTS = 16
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 10.0
PROBE_TIMEOUT_S = 0.3
START_TIMEOUT_S = 3.0
STOP_TIMEOUT_S = 2.0
SSE_RETRY_MS = 2000
THEME_CHECK_S = 1.0

EventSource = Callable[[Optional[int]], AsyncIterator[dict]]


class OverlayPageError(RuntimeError):
    """The page could not start. The message names the fix."""


def port_from_settings(settings: Mapping[str, Any]) -> int:
    raw = settings.get(KEY_PORT, DEFAULT_PORT)
    if raw in (None, ""):
        return DEFAULT_PORT
    return check_port(raw)


def check_port(raw: Any) -> int:
    try:
        port = int(raw)
    except (TypeError, ValueError):
        port = -1
    if isinstance(raw, bool) or not PORT_MIN <= port <= PORT_MAX:
        raise OverlayPageError(f"{KEY_PORT} must be a whole number from {PORT_MIN} to {PORT_MAX}, not {raw!r}")
    return port


def enabled_in(settings: Mapping[str, Any]) -> bool:
    value = settings.get(KEY_ENABLED, False)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return value is True


def page_settings(settings: Mapping[str, Any]) -> dict:
    """The shared overlay settings the page starts from (URL query parameters override them)."""
    out = asdict(overlay_settings.from_settings(settings))
    out.pop("auto", None)
    return out


def slim_event(event: dict) -> Optional[dict]:
    """Only what the page draws. Status details (source, metrics, messages) stay off the page."""
    kind = event.get("type")
    seq = event.get("seq")
    if kind == "hello":
        session = event.get("session") or {}
        return {"type": "hello", "seq": seq, "session": {"state": session.get("state")}}
    if kind == "status":
        return {"type": "status", "seq": seq, "state": event.get("state")}
    if kind == "line":
        line = event.get("line") or {}
        return {"type": "line", "seq": seq,
                "line": {k: line.get(k) for k in ("line_id", "state", "original", "translation", "speaker_id")}}
    if kind == "relabel":
        return {"type": "relabel", "seq": seq, "from": event.get("from"), "to": event.get("to")}
    if kind == "reset":
        return {"type": "reset", "seq": seq}
    return None


def sse_chunk(event: dict) -> bytes:
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    head = ""
    # hello carries the latest seq without being part of the sequence, so it never becomes
    # the Last-Event-ID a reconnect resumes from.
    if event.get("type") in ("line", "relabel", "status") and isinstance(event.get("seq"), int):
        head = f"id: {event['seq']}\n"
    return f"{head}data: {data}\n\n".encode("utf-8")


def _csp_hash(source: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii") + "'"


def _css_value(value: Any) -> str:
    text = str(value)
    if any(ch in text for ch in "{};<>\\"):
        raise OverlayPageError(f"overlay token value {text!r} is not a plain CSS value")
    return text


def _block(selector: str, props: Mapping[str, Any]) -> str:
    body = "".join(f"  {name}: {_css_value(value)};\n" for name, value in props.items())
    return f"{selector} {{\n{body}}}\n"


def theme_css(tokens: Mapping[str, Any]) -> str:
    """CSS custom properties from ``theme_tokens`` output.

    Without a palette from Desktop, light is the default and dark follows
    prefers-color-scheme. With one, Desktop's own light or dark is the default.
    ``?theme=light|dark`` sets ``data-theme`` on the root and forces that mode.
    """
    common, light, dark = tokens["common"], tokens["light"], tokens["dark"]
    if tokens.get("follow_system", True):
        return (_block(":root", {**common, **light})
                + "@media (prefers-color-scheme: dark) {\n"
                + _block(':root:not([data-theme="light"])', dark)
                + "}\n"
                + _block(':root[data-theme="dark"]', dark))
    return (_block(":root", {**common, **tokens["default"]})
            + _block(':root[data-theme="light"]', light)
            + _block(':root[data-theme="dark"]', dark))


# Text sizes relative to the translation. The native strip uses the same two numbers
# (overlay/panel_mac.py ORIGINAL_SCALE and LABEL_SCALE); they are layout, not theme tokens.
ORIGINAL_SCALE = 0.58
LABEL_SCALE = 0.62


def _scheme_vars(pal: Any) -> dict:
    out = {"--surface": pal.surface.css(), "--text-primary": pal.text.css(),
           "--text-secondary": pal.text_secondary.css(), "--text-pending": pal.text_secondary.css(),
           "--text-failed": pal.text_secondary.css(), "--failed": pal.failed_color.css(), "--font-sans": f"{pal.font}, system-ui, sans-serif",
           "--radius": f"{pal.radius_px:g}px"}
    out.update({f"--speaker-{i + 1}": c.css() for i, c in enumerate(pal.speakers)})
    return out


def theme_tokens(theme_file: Optional[Path] = None) -> dict:
    """The page's CSS variables from the shared overlay token table (``overlay_tokens.py``).

    The native overlay draws from the same table, so both show the same colours,
    weights and radius. When the Desktop pane has sent the active theme
    (``theme_file``, ``overlay/theme.json``), that palette is used, in Desktop's
    own light or dark; otherwise the nous fallback for the system appearance.
    """
    from . import overlay_tokens as tokens

    sent = None
    if theme_file is not None:
        try:
            sent = tokens.palette_from_json(Path(theme_file).read_text(encoding="utf-8"))
        except OSError:
            sent = None
    light = sent if sent is not None and not sent.dark else tokens.FALLBACK[False]
    dark = sent if sent is not None and sent.dark else tokens.FALLBACK[True]
    common = {"--label-weight": str(tokens.WEIGHT_LABEL), "--text-weight": str(tokens.WEIGHT_TEXT),
              "--label-scale": f"{LABEL_SCALE}em", "--original-scale": f"{ORIGINAL_SCALE}em",
              "--original-order": "-1"}
    out = {"common": common, "light": _scheme_vars(light), "dark": _scheme_vars(dark),
           "follow_system": sent is None}
    out["default"] = _scheme_vars(sent) if sent is not None else out["light"]
    return out


def load_page(path: Path = PAGE_FILE) -> str:
    """The page template. Its CSP is made per response (render_page), because the theme is written in."""
    html = path.read_text(encoding="utf-8")
    if (SETTINGS_PLACEHOLDER not in html or THEME_PLACEHOLDER not in html
            or len(re.findall(r"<script>(.*?)</script>", html, flags=re.S)) != 1
            or len(re.findall(r"<style>(.*?)</style>", html, flags=re.S)) != 1):
        raise OverlayPageError(f"{path} is not the overlay page template")
    return html


def render_page(template: str, settings: Mapping[str, Any], css: str = "") -> tuple[bytes, str]:
    """The page with its settings and theme written in, and the Content-Security-Policy that allows
    exactly its inline script and style (by hash)."""
    data = json.dumps(dict(settings), ensure_ascii=True, separators=(",", ":"))
    # The JSON sits in a <script type="application/json"> block; "<" never reaches the HTML parser.
    data = data.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    if "</" in css or "<" in css:
        raise OverlayPageError("overlay theme CSS may not contain '<'")
    html = template.replace(SETTINGS_PLACEHOLDER, data, 1).replace(THEME_PLACEHOLDER, css, 1)
    script = re.search(r"<script>(.*?)</script>", html, flags=re.S).group(1)
    style = re.search(r"<style>(.*?)</style>", html, flags=re.S).group(1)
    csp = ("default-src 'none'; "
           f"script-src {_csp_hash(script)}; style-src {_csp_hash(style)}; "
           "connect-src 'self'; img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
    return html.encode("utf-8"), csp


_REASONS = {200: "OK", 308: "Permanent Redirect", 400: "Bad Request", 403: "Forbidden", 404: "Not Found",
            405: "Method Not Allowed", 503: "Service Unavailable"}
_COMMON_HEADERS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"),
                   ("Referrer-Policy", "no-referrer"), ("Connection", "close"))


def _head(status: int, headers: list[tuple[str, str]]) -> bytes:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'Error')}"]
    lines += [f"{k}: {v}" for k, v in (*headers, *_COMMON_HEADERS)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def parse_head(raw: bytes) -> Optional[tuple[str, str, dict]]:
    try:
        text = raw.decode("latin-1")
    except UnicodeDecodeError:
        return None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return None
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(":")
        if not sep:
            return None
        key = name.strip().lower()
        if key in headers and key == "host":
            return None  # two Host headers: refuse rather than guess which one counts
        headers[key] = value.strip()
    return parts[0], parts[1], headers


def host_allowed(host: Optional[str], port: int) -> bool:
    if not host:
        return False
    return host.lower() in (f"127.0.0.1:{port}", f"localhost:{port}")


def _port_in_use(port: int) -> bool:
    """True when something accepts connections on 127.0.0.1:<port>.

    The listening socket uses SO_REUSEADDR so that a restart is not blocked by
    connections still in TIME_WAIT. On macOS that option also lets a bind to
    127.0.0.1 succeed beside another program's wildcard (all interfaces) listener, so a connect
    probe finds that program first.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(PROBE_TIMEOUT_S)
        return probe.connect_ex((HOST, port)) == 0


def _busy_message(port: int) -> str:
    return (f"port {port} on 127.0.0.1 is already in use by another program, so the overlay page did not start. "
            f"Pick a free port with: hermes hermeneus overlay-page on --port <port>")


def bind_loopback(port: int) -> socket.socket:
    if port and _port_in_use(port):
        raise OverlayPageError(_busy_message(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, port))
        sock.listen(MAX_CLIENTS)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            raise OverlayPageError(_busy_message(port)) from None
        raise OverlayPageError(f"the overlay page could not listen on 127.0.0.1:{port}: {exc}") from None
    return sock


class OverlayPageServer:
    """The HTTP server, on its own thread and event loop."""

    def __init__(self, *, port: int, events: EventSource, settings: Callable[[], Mapping[str, Any]],
                 token: Optional[str] = None, page_file: Path = PAGE_FILE,
                 theme_file: Optional[Callable[[], Optional[Path]]] = None) -> None:
        self.requested_port = port
        self.port = port
        # (host, port) the listening socket reports once bound; host is always 127.0.0.1.
        self.bound_address: Optional[tuple] = None
        self.token = token or secrets.token_urlsafe(24)
        self._events = events
        self._settings = settings
        self._template = load_page(page_file)
        self._theme_file = theme_file or (lambda: None)
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stopping: Optional[asyncio.Event] = None
        self._clients: set[asyncio.Task] = set()
        self._streams = 0

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/{self.token}/"

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bind, then serve on a new thread. Raises OverlayPageError (the bind happens here, on the caller)."""
        sock = bind_loopback(self.requested_port)
        self.bound_address = sock.getsockname()
        self.port = self.bound_address[1]
        ready = threading.Event()
        failure: list[BaseException] = []

        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self._stopping = asyncio.Event()
            try:
                server = await asyncio.start_server(self._handle, sock=sock, limit=MAX_HEAD_BYTES)
            except BaseException as exc:
                failure.append(exc)
                ready.set()
                sock.close()
                return
            ready.set()
            try:
                await self._stopping.wait()
            finally:
                server.close()
                for task in list(self._clients):
                    task.cancel()
                for task in list(self._clients):
                    with contextlib.suppress(BaseException):
                        await task
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server.wait_closed(), 1.0)

        def thread_main() -> None:
            try:
                asyncio.run(main())
            except BaseException:
                logger.exception("hermeneus: the overlay page thread ended with an error")
            finally:
                self._loop = None
                ready.set()

        self._thread = threading.Thread(target=thread_main, name="hermeneus-overlay-page", daemon=True)
        self._thread.start()
        if not ready.wait(START_TIMEOUT_S) or failure:
            self.stop()
            raise OverlayPageError(f"the overlay page could not start: {failure[0] if failure else 'timeout'}")

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> bool:
        thread, loop, stopping = self._thread, self._loop, self._stopping
        if thread is None:
            return True
        if loop is not None and stopping is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stopping.set)
        thread.join(timeout)
        return not thread.is_alive()

    # -- HTTP ------------------------------------------------------------------

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            try:
                raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT_S)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ValueError):
                return
            request = parse_head(raw)
            if request is None:
                await self._plain(writer, 400, "bad request")
                return
            await self._route(writer, *request)
        except (ConnectionError, asyncio.CancelledError, asyncio.TimeoutError):
            pass
        finally:
            self._clients.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _plain(self, writer: asyncio.StreamWriter, status: int, text: str, *, head_only: bool = False,
                     extra: tuple[tuple[str, str], ...] = ()) -> None:
        body = (text + "\n").encode("utf-8")
        writer.write(_head(status, [("Content-Type", "text/plain; charset=utf-8"),
                                    ("Content-Length", str(len(body))), *extra]))
        if not head_only:
            writer.write(body)
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)

    async def _route(self, writer: asyncio.StreamWriter, method: str, target: str, headers: dict) -> None:
        head_only = method == "HEAD"
        if method not in ("GET", "HEAD"):
            await self._plain(writer, 405, "method not allowed", extra=(("Allow", "GET, HEAD"),))
            return
        if not host_allowed(headers.get("host"), self.port):
            await self._plain(writer, 403, "host not allowed; open the page on 127.0.0.1", head_only=head_only)
            return
        split = urlsplit(target)
        parts = split.path.split("/")
        given = parts[1] if len(parts) > 1 else ""
        if not hmac.compare_digest(given.encode("utf-8"), self.token.encode("utf-8")):
            await self._plain(writer, 403, "the address needs the token that hermes hermeneus overlay-page prints",
                              head_only=head_only)
            return
        rest = "/".join(parts[2:])
        if len(parts) == 2:
            location = f"/{self.token}/" + (f"?{split.query}" if split.query else "")
            await self._plain(writer, 308, "moved", head_only=head_only, extra=(("Location", location),))
        elif rest == "":
            await self._page(writer, head_only)
        elif rest == "events":
            await self._stream(writer, headers, split.query, head_only)
        else:
            await self._plain(writer, 404, "not found", head_only=head_only)

    async def _page(self, writer: asyncio.StreamWriter, head_only: bool) -> None:
        try:
            settings = await asyncio.to_thread(lambda: page_settings(self._settings()))
        except Exception as exc:  # unreadable config: the page still works with its own defaults
            logger.warning("hermeneus: overlay settings could not be read: %s", exc)
            settings = page_settings({})
        try:
            css = await asyncio.to_thread(lambda: theme_css(theme_tokens(self._theme_file())))
        except OverlayPageError as exc:  # a stored theme with a value that is not plain CSS
            logger.warning("hermeneus: overlay theme refused: %s", exc)
            css = theme_css(theme_tokens(None))
        body, csp = render_page(self._template, settings, css)
        writer.write(_head(200, [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
                                 ("Content-Security-Policy", csp)]))
        if not head_only:
            writer.write(body)
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)

    async def _stream(self, writer: asyncio.StreamWriter, headers: dict, query: str, head_only: bool) -> None:
        if self._streams >= MAX_CLIENTS:
            await self._plain(writer, 503, "too many overlay pages are open", head_only=head_only)
            return
        after = _resume_seq(headers.get("last-event-id"), parse_qs(query).get("after", [None])[0])
        writer.write(_head(200, [("Content-Type", "text/event-stream; charset=utf-8")]))
        if head_only:
            await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
            return
        writer.write(f"retry: {SSE_RETRY_MS}\n\n".encode("ascii"))
        await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
        self._streams += 1
        stream = self._events(after)
        seen = _mtime(self._theme_file())
        checked = time.monotonic()
        try:
            async for event in stream:
                slim = slim_event(event)
                if slim is not None:
                    writer.write(sse_chunk(slim))
                # The pane sends a new theme when Desktop's theme changes; pass it on.
                if time.monotonic() - checked >= THEME_CHECK_S:
                    checked = time.monotonic()
                    path = self._theme_file()
                    now = _mtime(path)
                    if now != seen:
                        seen = now
                        theme = self._theme_event(path)
                        if theme is not None:
                            writer.write(sse_chunk(theme))
                await asyncio.wait_for(writer.drain(), WRITE_TIMEOUT_S)
        finally:
            self._streams -= 1
            with contextlib.suppress(Exception):
                await stream.aclose()


    @staticmethod
    def _theme_event(path: Optional[Path]) -> Optional[dict]:
        try:
            tokens = theme_tokens(path)
            theme_css(tokens)  # every value is plain CSS, or nothing is sent
        except OverlayPageError as exc:
            logger.warning("hermeneus: overlay theme refused: %s", exc)
            return None
        return {"type": "theme", "tokens": tokens}


def _mtime(path: Optional[Path]) -> Optional[int]:
    if path is None:
        return None
    try:
        return Path(path).stat().st_mtime_ns
    except OSError:
        return None


def _resume_seq(last_event_id: Optional[str], after: Optional[str]) -> int:
    """Where a stream starts: the browser's Last-Event-ID, then ?after=, else 0 (every stored line)."""
    for raw in (last_event_id, after):
        if raw is None:
            continue
        try:
            return max(int(raw), 0)
        except ValueError:
            continue
    return 0


def _overlay_theme_file(data_dir: Path) -> Path:
    """Where the Desktop pane stores the active theme for both overlays."""
    from .overlay.control import theme_file
    return theme_file(data_dir)


def controller_events(data_dir: Callable[[], Path]) -> EventSource:
    """Events through ``Controller.subscribe`` over the session's Unix socket."""
    from .control import Controller

    def events(after: Optional[int]) -> AsyncIterator[dict]:
        return Controller(data_dir, owner="overlay-page").subscribe(after)

    return events


class OverlayPage:
    """The switch the session owner holds: starts and stops the server and answers what it serves.

    ``switch`` is thread safe. ``info()`` is what the command, the doctor and
    the pane show: ``{"type": "overlay", "running", "url", "port", "error"}``.
    """

    def __init__(self, data_dir: Callable[[], Path], settings: Callable[[], Mapping[str, Any]], *,
                 events: Optional[EventSource] = None) -> None:
        self._settings = settings
        self._events = events or controller_events(data_dir)
        self._theme_file = lambda: _overlay_theme_file(data_dir())
        self._server: Optional[OverlayPageServer] = None
        self._error = ""
        self._mutex = threading.Lock()

    def info(self) -> dict:
        server = self._server
        running = server is not None and server.running
        return {"type": "overlay", "running": running, "url": server.url if running else None,
                "port": server.port if running else None, "error": self._error}

    def auto_start(self) -> dict:
        """Start the page when the setting is on. Never raises; a failure is in ``info()["error"]``."""
        try:
            on = enabled_in(self._settings())
        except Exception as exc:
            self._error = f"the overlay page setting could not be read: {exc}"
            return self.info()
        return self.switch(True) if on else self.info()

    def switch(self, on: Optional[bool], port: Optional[int] = None) -> dict:
        with self._mutex:
            if on is True:
                self._start(port)
            elif on is False:
                self._stop()
                self._error = ""
            return self.info()

    def stop(self) -> None:
        with self._mutex:
            self._stop()

    def _start(self, port: Optional[int]) -> None:
        try:
            wanted = check_port(port) if port is not None else port_from_settings(self._settings())
        except OverlayPageError as exc:
            self._error = str(exc)
            return
        current = self._server
        if current is not None and current.running and current.port == wanted:
            return
        self._stop()
        try:
            server = OverlayPageServer(port=wanted, events=self._events, settings=self._settings,
                                       theme_file=self._theme_file)
            server.start()
        except (OverlayPageError, OSError) as exc:
            self._error = str(exc)
            return
        self._server = server
        self._error = ""

    def _stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.stop()
