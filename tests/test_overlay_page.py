"""The 127.0.0.1 overlay page: loopback bind, token, Host check, event relay and lifetime."""

import argparse
import asyncio
import io
import json
import socket

import pytest

from pipeline_fakes import make_rig, options, remove_tmpdir, short_tmpdir, wait_for
from plugin_loader import load_plugin_package

from hermeneus_engine import overlay_page
from hermeneus_engine.contracts import CommittedTranscript, PartialTranscript
from hermeneus_engine.control import Controller
from hermeneus_engine.runtime import SessionRuntime

pkg = load_plugin_package()
commands = pkg.commands


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, path, *, host=None, method="GET", headers=(), read_all=True, timeout=3.0):
    """One raw HTTP/1.1 request. Returns (status, headers, body)."""
    host = host or f"127.0.0.1:{port}"
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}", *headers, "", ""]
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall("\r\n".join(lines).encode("latin-1"))
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(65536)
            if not chunk:
                break
            data += chunk
        if read_all:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    hdrs = {}
    for line in header_lines:
        k, _, v = line.partition(":")
        hdrs[k.strip().lower()] = v.strip()
    return int(status_line.split(" ")[1]), hdrs, body


def read_sse(port, path, *, until, headers=(), timeout=5.0):
    """Read SSE events from ``path`` until ``until(events)`` is true."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n{''.join(h + chr(13) + chr(10) for h in headers)}\r\n"
                  .encode("latin-1"))
        buf = b""
        events, ids = [], []
        while True:
            chunk = s.recv(65536)
            if not chunk:
                return events, ids, buf
            buf += chunk
            head, sep, body = buf.partition(b"\r\n\r\n")
            if not sep:
                continue
            events, ids = [], []
            for block in body.decode("utf-8").split("\n\n"):
                for line in block.split("\n"):
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
                    elif line.startswith("id: "):
                        ids.append(int(line[4:]))
            if until(events):
                return events, ids, buf


async def fake_events(after):
    yield {"type": "hello", "version": 1, "seq": 5, "session": {"state": "live", "metrics": {"x": 1}}}
    yield {"type": "line", "seq": 6, "line": {"line_id": "a", "state": "translated", "original": "hello",
                                              "translation": "merhaba", "speaker_id": "s1:speaker_0",
                                              "source_language": "en"}}
    yield {"type": "status", "seq": 7, "state": "live", "message": "secret detail", "metrics": {"x": 1},
           "source": {"label": "Safari"}}
    await asyncio.Event().wait()


@pytest.fixture
def server():
    srv = overlay_page.OverlayPageServer(port=0, events=fake_events, settings=lambda: {"overlay_max_lines": 3})
    srv.start()
    yield srv
    assert srv.stop()


# -- server --------------------------------------------------------------------------

def test_server_binds_loopback_only(server):
    sock_name = None
    # The listening address the kernel reports for the server's port.
    with socket.create_connection(("127.0.0.1", server.port), timeout=2) as c:
        sock_name = c.getpeername()
    assert sock_name == ("127.0.0.1", server.port)
    assert server.url == f"http://127.0.0.1:{server.port}/{server.token}/"
    assert overlay_page.HOST == "127.0.0.1"
    # Reaching the same port through any non-loopback address of this Mac is refused.
    # The address of the default route's interface. A UDP connect sends no packet.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.connect(("192.0.2.1", 9))
            lan = u.getsockname()[0]
    except OSError:
        lan = None
    if lan and not lan.startswith("127."):
        with pytest.raises(OSError):
            socket.create_connection((lan, server.port), timeout=1).close()


def test_token_is_required(server):
    assert http(server.port, "/")[0] == 403
    assert http(server.port, "/favicon.ico")[0] == 403
    assert http(server.port, "/wrong-token/")[0] == 403
    assert http(server.port, "/wrong-token/events")[0] == 403
    assert http(server.port, f"/{server.token[:-1]}/")[0] == 403
    status, hdrs, body = http(server.port, f"/{server.token}/")
    assert status == 200
    assert hdrs["content-type"].startswith("text/html")
    assert b"HermeneusOverlay" in body
    assert http(server.port, f"/{server.token}?max_lines=1")[1]["location"] == f"/{server.token}/?max_lines=1"
    assert http(server.port, f"/{server.token}/other")[0] == 404


def test_two_servers_get_different_tokens():
    a = overlay_page.OverlayPageServer(port=0, events=fake_events, settings=dict)
    b = overlay_page.OverlayPageServer(port=0, events=fake_events, settings=dict)
    assert a.token != b.token and len(a.token) >= 32


def test_host_header_is_checked(server):
    token = server.token
    assert http(server.port, f"/{token}/", host=f"evil.example:{server.port}")[0] == 403
    assert http(server.port, f"/{token}/", host="127.0.0.1")[0] == 403
    assert http(server.port, f"/{token}/", host=f"127.0.0.1:{server.port + 1}")[0] == 403
    assert http(server.port, f"/{token}/", host=f"localhost:{server.port}")[0] == 200
    assert http(server.port, f"/{token}/", host=f"LOCALHOST:{server.port}")[0] == 200


def test_read_only_and_no_cors(server):
    status, hdrs, _ = http(server.port, f"/{server.token}/", method="POST")
    assert status == 405 and hdrs["allow"] == "GET, HEAD"
    assert http(server.port, f"/{server.token}/events", method="DELETE")[0] == 405
    status, hdrs, body = http(server.port, f"/{server.token}/", method="HEAD")
    assert status == 200 and body == b""
    status, hdrs, _ = http(server.port, f"/{server.token}/", headers=("Origin: https://evil.example",))
    assert status == 200
    assert not [k for k in hdrs if k.startswith("access-control-")]
    assert hdrs["cache-control"] == "no-store"
    assert hdrs["referrer-policy"] == "no-referrer"
    csp = hdrs["content-security-policy"]
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp and "'unsafe-inline'" not in csp


def test_page_is_self_contained_and_carries_the_settings(server):
    _, hdrs, body = http(server.port, f"/{server.token}/")
    html = body.decode("utf-8")
    assert "http://" not in html.replace("http://127.0.0.1", "") and "https://" not in html
    assert '"max_lines":3' in html and '"show_original":true' in html
    assert overlay_page.SETTINGS_PLACEHOLDER not in html


def test_settings_cannot_break_out_of_the_json_block():
    template = overlay_page.load_page()
    body, _ = overlay_page.render_page(template, {"x": "</script><script>alert(1)</script>"})
    html = body.decode()
    assert "</script><script>alert(1)" not in html
    assert "\\u003c/script\\u003e" in html


def test_theme_is_written_in_and_allowed_by_hash(server):
    import base64, hashlib, re
    _, hdrs, body = http(server.port, f"/{server.token}/")
    html = body.decode()
    style = re.search(r"<style>(.*?)</style>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"style-src 'sha256-{digest}'" in hdrs["content-security-policy"]
    assert overlay_page.THEME_PLACEHOLDER not in html
    assert "--surface:" in style and ':root[data-theme="dark"]' in style


def test_page_uses_the_shared_token_table():
    from hermeneus_engine import overlay_tokens as table
    tokens = overlay_page.theme_tokens(None)
    assert tokens["follow_system"] is True
    for dark, mode in ((False, "light"), (True, "dark")):
        pal = table.FALLBACK[dark]
        assert tokens[mode]["--surface"] == pal.surface.css()
        assert tokens[mode]["--text-primary"] == pal.text.css()
        assert tokens[mode]["--text-pending"] == pal.text_secondary.css()
        assert tokens[mode]["--speaker-1"] == pal.speakers[0].css()
        assert tokens[mode]["--radius"] == f"{pal.radius_px:g}px"
    assert tokens["common"]["--label-weight"] == str(table.WEIGHT_LABEL)
    assert tokens["common"]["--text-weight"] == str(table.WEIGHT_TEXT)
    css = overlay_page.theme_css(tokens)
    assert "prefers-color-scheme: dark" in css


def sent_palette(tmp_path, dark=True, surface="#203040"):
    from hermeneus_engine import overlay_tokens as table
    pal = table.palette_from_payload({"mode": "dark" if dark else "light", "name": "mono",
                                      "colors": {"surface": surface, "text": "rgb(250, 250, 250)",
                                                 "text_secondary": "rgba(250, 250, 250, 0.74)",
                                                 "text_tertiary": "rgba(250, 250, 250, 0.54)",
                                                 "stroke": "rgba(250, 250, 250, 0.1)",
                                                 "speakers": ["#ff0000", "#00ff00"]},
                                      "radius_px": 6, "font": "Menlo"})
    path = tmp_path / "theme.json"
    path.write_text(table.palette_to_json(pal), encoding="utf-8")
    return path


def test_a_theme_sent_by_desktop_wins(tmp_path):
    from hermeneus_engine import overlay_tokens as table
    tokens = overlay_page.theme_tokens(sent_palette(tmp_path))
    assert tokens["follow_system"] is False
    assert tokens["default"]["--surface"] == table.Rgba.hex("#203040").css()
    assert tokens["dark"] == tokens["default"]
    assert tokens["light"]["--surface"] == table.FALLBACK[False].surface.css()
    assert tokens["default"]["--speaker-2"] == table.Rgba.hex("#00ff00").css()
    assert tokens["default"]["--radius"] == "6px"
    css = overlay_page.theme_css(tokens)
    assert "prefers-color-scheme" not in css and ':root[data-theme="light"]' in css
    (tmp_path / "theme.json").write_text("not json", encoding="utf-8")
    assert overlay_page.theme_tokens(tmp_path / "theme.json")["follow_system"] is True


def test_theme_changes_reach_open_pages(tmp_path):
    path = tmp_path / "theme.json"

    async def events(after):
        for seq in range(1, 400):
            yield {"type": "status", "seq": seq, "state": "live"}
            await asyncio.sleep(0.02)

    srv = overlay_page.OverlayPageServer(port=0, events=events, settings=dict, theme_file=lambda: path)
    srv.start()
    try:
        import threading, time
        threading.Timer(0.3, lambda: sent_palette(tmp_path, surface="#405060")).start()
        found, _, _ = read_sse(srv.port, f"/{srv.token}/events",
                               until=lambda ev: any(e["type"] == "theme" for e in ev))
        from hermeneus_engine.overlay_tokens import Rgba
        want = Rgba.hex("#405060").css()
        theme = next(e for e in found if e["type"] == "theme")
        assert theme["tokens"]["default"]["--surface"] == want
        _, hdrs, body = http(srv.port, f"/{srv.token}/")
        assert want.encode() in body
    finally:
        srv.stop()


def test_theme_values_cannot_inject_css():
    for bad in ("red; } body { display: none", "x</style>", "a{b}"):
        with pytest.raises(overlay_page.OverlayPageError):
            overlay_page.theme_css({**overlay_page.theme_tokens(None), "dark": {"--surface": bad}})


def test_events_stream_is_sse_with_slim_events(server):
    events, ids, raw = read_sse(server.port, f"/{server.token}/events",
                                until=lambda ev: any(e["type"] == "status" for e in ev))
    assert b"text/event-stream" in raw and b"retry: 2000" in raw
    assert [e["type"] for e in events] == ["hello", "line", "status"]
    assert events[0] == {"type": "hello", "seq": 5, "session": {"state": "live"}}
    assert events[1]["line"] == {"line_id": "a", "state": "translated", "original": "hello",
                                 "translation": "merhaba", "speaker_id": "s1:speaker_0"}
    assert events[2] == {"type": "status", "seq": 7, "state": "live"}
    # hello is not part of the sequence, so it never becomes the Last-Event-ID.
    assert ids == [6, 7]


def test_resume_uses_last_event_id():
    seen = []

    async def events(after):
        seen.append(after)
        yield {"type": "status", "seq": 9, "state": "live"}
        await asyncio.Event().wait()

    srv = overlay_page.OverlayPageServer(port=0, events=events, settings=dict)
    srv.start()
    try:
        read_sse(srv.port, f"/{srv.token}/events", until=bool)
        read_sse(srv.port, f"/{srv.token}/events", headers=("Last-Event-ID: 42",), until=bool)
        read_sse(srv.port, f"/{srv.token}/events?after=7", until=bool)
    finally:
        srv.stop()
    assert seen == [0, 42, 7]


def test_a_busy_port_is_an_error_not_another_port():
    for address in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as other:
            other.bind((address, 0))
            other.listen(1)
            port = other.getsockname()[1]
            srv = overlay_page.OverlayPageServer(port=port, events=fake_events, settings=dict)
            with pytest.raises(overlay_page.OverlayPageError, match=f"port {port} on 127.0.0.1 is already in use"):
                srv.start()
            assert not srv.running


def test_restart_on_the_same_port_after_connections(server):
    port = server.port
    http(server.port, f"/{server.token}/")
    assert server.stop()
    again = overlay_page.OverlayPageServer(port=port, events=fake_events, settings=dict)
    again.start()
    try:
        assert http(port, f"/{again.token}/")[0] == 200
        assert http(port, f"/{server.token}/")[0] == 403
    finally:
        again.stop()


def test_port_setting_is_checked():
    assert overlay_page.port_from_settings({}) == overlay_page.DEFAULT_PORT
    assert overlay_page.port_from_settings({"overlay_page_port": "47830"}) == 47830
    for bad in (80, 70000, "x", True):
        with pytest.raises(overlay_page.OverlayPageError):
            overlay_page.port_from_settings({"overlay_page_port": bad})


# -- with a session ----------------------------------------------------------------

def page_settings(port, **extra):
    return lambda: {"overlay_page": True, "overlay_page_port": port, **extra}


async def test_relays_a_fake_session_and_stops_with_it(data_dir):
    port = free_port()
    rig = make_rig()
    page = overlay_page.OverlayPage(lambda: data_dir, page_settings(port))
    rt = SessionRuntime(data_dir, options(), rig.build, owner="test", overlay_page=page)
    await rt.start()
    try:
        info = page.info()
        assert info["running"] and info["port"] == port and info["error"] == ""
        url_path = info["url"].split(f":{port}", 1)[1]
        await wait_for(lambda: rt.bus.status()["state"] == "live")
        reader = asyncio.create_task(asyncio.to_thread(
            read_sse, port, url_path + "events",
            until=lambda ev: any(e["type"] == "line" and e["line"]["state"] == "translated" for e in ev)))
        await asyncio.sleep(0.3)
        rig.stt.emit(PartialTranscript("hello wor"))
        rig.stt.emit(CommittedTranscript("hello world", segment_index=0))
        events, _, _ = await asyncio.wait_for(reader, 5)
        lines = [e["line"] for e in events if e["type"] == "line"]
        assert lines[-1]["state"] == "translated" and lines[-1]["translation"] == "<hello world>"
        assert all(set(e) <= {"type", "seq", "state"} for e in events if e["type"] == "status")
    finally:
        await rt.close()
    assert not page.info()["running"]
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


async def test_session_ending_by_itself_stops_the_page(data_dir):
    port = free_port()
    rig = make_rig()
    page = overlay_page.OverlayPage(lambda: data_dir, page_settings(port))
    rt = SessionRuntime(data_dir, options(), rig.build, owner="test", overlay_page=page)
    await rt.start()
    assert page.info()["running"]
    from hermeneus_engine.contracts import SttState, SttStatus
    rig.stt.emit(SttStatus(SttState.FAILED, message="quota exceeded"))
    await asyncio.wait_for(rt.wait(), 3)
    assert not page.info()["running"]


async def test_off_by_default(data_dir):
    page = overlay_page.OverlayPage(lambda: data_dir, lambda: {})
    rt = SessionRuntime(data_dir, options(), make_rig().build, owner="test", overlay_page=page)
    await rt.start()
    try:
        assert page.info() == {"type": "overlay", "running": False, "url": None, "port": None, "error": ""}
    finally:
        await rt.close()


async def test_switch_over_the_unix_socket_and_a_new_token_each_start(data_dir):
    port = free_port()
    settings = {"overlay_page": False, "overlay_page_port": port}
    page = overlay_page.OverlayPage(lambda: data_dir, lambda: dict(settings))
    rt = SessionRuntime(data_dir, options(), make_rig().build, owner="test", overlay_page=page)
    await rt.start()
    try:
        other = Controller(lambda: data_dir, owner="other")
        assert (await other.overlay())["running"] is False
        first = await other.overlay(True)
        assert first["running"] and first["port"] == port
        second_port = free_port()
        moved = await other.overlay(True, second_port)
        assert moved["port"] == second_port
        assert (await other.overlay(False))["running"] is False
        again = await other.overlay(True, port)
        assert again["url"] != first["url"]
    finally:
        await rt.close()
    assert await Controller(lambda: data_dir, owner="other").overlay() is None


async def test_busy_port_during_a_session_is_reported(data_dir):
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = other.getsockname()[1]
        page = overlay_page.OverlayPage(lambda: data_dir, page_settings(port))
        rt = SessionRuntime(data_dir, options(), make_rig().build, owner="test", overlay_page=page)
        await rt.start()
        try:
            info = page.info()
            assert not info["running"] and "already in use" in info["error"]
            assert rt.bus.status()["state"] in ("starting", "live")  # the session itself is unaffected
        finally:
            await rt.close()


# -- command and doctor ----------------------------------------------------------------

def overlay_args(**kw):
    ns = argparse.Namespace(switch=None, port=None, json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def make_plugin(data_dir, settings):
    saved = {}

    def save(key, value):
        saved[key] = value
        settings[key] = value

    plugin = commands.Plugin(None, owner="test", settings=lambda: dict(settings), data_dir=lambda: data_dir,
                             build=make_rig().build, list_sources=lambda: ([options().source], []),
                             save_setting=save)
    return plugin, saved


def test_command_without_a_session_saves_the_setting(data_dir):
    settings = {}
    plugin, saved = make_plugin(data_dir, settings)
    out = io.StringIO()
    assert commands.cmd_overlay_page(plugin, overlay_args(), out=out) == 0
    assert "off" in out.getvalue()
    out = io.StringIO()
    assert commands.cmd_overlay_page(plugin, overlay_args(switch="on", port=47830), out=out) == 0
    assert saved == {"overlay_page_port": 47830, "overlay_page": True}
    assert "starts with the next session on 127.0.0.1:47830" in out.getvalue()
    err = io.StringIO()
    assert commands.cmd_overlay_page(plugin, overlay_args(port=47831), err=err) == 2
    assert commands.cmd_overlay_page(plugin, overlay_args(switch="on", port=80), err=err) == 2


async def test_command_switches_a_running_session_and_prints_the_url(data_dir):
    port = free_port()
    settings = {"overlay_page": False, "overlay_page_port": port}
    plugin, _ = make_plugin(data_dir, settings)
    await plugin.start(commands.Choices(to="tr", source="BlackHole"))
    try:
        out = io.StringIO()
        code = await asyncio.to_thread(commands.cmd_overlay_page, plugin, overlay_args(switch="on"), out=out)
        assert code == 0
        assert f"http://127.0.0.1:{port}/" in out.getvalue()
        out = io.StringIO()
        await asyncio.to_thread(commands.cmd_overlay_page, plugin, overlay_args(json=True), out=out)
        body = json.loads(out.getvalue())
        assert body["enabled"] and body["running"] and body["url"].startswith(f"http://127.0.0.1:{port}/")
        check = commands.overlay_check(plugin.settings(), await plugin.controller.overlay())
        assert check.ok and body["url"] in check.detail
        out = io.StringIO()
        await asyncio.to_thread(commands.cmd_overlay_page, plugin, overlay_args(switch="off"), out=out)
        assert "off" in out.getvalue()
    finally:
        await plugin.controller.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


async def test_plugin_unload_stops_the_page(data_dir):
    port = free_port()
    plugin, _ = make_plugin(data_dir, {"overlay_page": True, "overlay_page_port": port})
    await plugin.start(commands.Choices(to="tr", source="BlackHole"))
    assert (await plugin.controller.overlay())["running"]
    await asyncio.to_thread(plugin.unload)
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


def test_doctor_lines():
    off = commands.overlay_check({}, None)
    assert off.ok and "off" in off.detail and not off.required
    waiting = commands.overlay_check({"overlay_page": True, "overlay_page_port": 47830}, None)
    assert waiting.ok and "47830" in waiting.detail
    failed = commands.overlay_check({"overlay_page": True}, {"running": False, "error": "port 1 busy"})
    assert failed.ok is False and failed.state == "warn" and "--port" in failed.fix
