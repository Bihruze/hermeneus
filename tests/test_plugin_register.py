import argparse
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import pytest

from pipeline_fakes import SOURCE, FakeCtx, FakeLlm, make_rig, options, remove_tmpdir, short_tmpdir, wait_for
from plugin_loader import ROOT, load_plugin_package, share_engine

from hermeneus_engine.runtime import SessionRuntime, lock_holder, lock_path, socket_path

pkg = load_plugin_package()


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def test_register_declares_exactly_what_the_manifest_says():
    ctx = FakeCtx()
    pkg.register(ctx)
    assert [k for k, _ in ctx.aux_tasks] == ["live_translate"]
    assert set(ctx.commands) == {"translate"}
    assert set(ctx.cli_commands) == {"hermeneus"}
    assert ctx.tools == [] and ctx.hooks == []  # plugin.yaml: provides_tools: [], provides_hooks: []
    assert len(ctx.unload_callbacks) == 1


def test_cli_parser_has_doctor_devices_run_without_prompts():
    ctx = FakeCtx()
    pkg.register(ctx)
    parser = argparse.ArgumentParser(prog="hermes hermeneus")
    ctx.cli_commands["hermeneus"]["setup_fn"](parser)
    args = parser.parse_args(["run", "--to", "tr", "--source", "BlackHole", "--speak", "--json"])
    assert (args.hermeneus_command, args.to, args.speak, args.json) == ("run", "tr", True, True)
    assert parser.parse_args(["doctor"]).hermeneus_command == "doctor"


async def test_llm_call_uses_the_registered_task_and_never_picks_a_model():
    llm = FakeLlm("merhaba")
    call = pkg.glue.make_llm_call(llm)
    assert await call([{"role": "user", "content": "hello"}]) == "merhaba"
    [kw] = llm.calls
    assert kw["task"] == "live_translate" and kw["timeout"] == 6.0
    assert "provider" not in kw and "model" not in kw


def test_on_unload_stops_the_local_session_and_removes_socket_and_lock(data_dir):
    rig = make_rig()
    plugin = pkg.commands.Plugin(None, owner="test", settings=lambda: {}, data_dir=lambda: data_dir,
                                 build=rig.build, list_sources=lambda: ([SOURCE], []))
    out = asyncio.run(plugin.slash("start --to tr --source BlackHole"))
    assert "Translation is" in out
    assert lock_holder(data_dir) is not None and socket_path(data_dir).exists()
    plugin.unload()
    assert lock_holder(data_dir) is None
    assert not socket_path(data_dir).exists()
    assert lock_path(data_dir).read_text() == ""  # pid info cleared on release
    assert rig.capture.stopped == 1 and rig.stt.closed


def test_settings_come_from_ctx_get_config_with_defaults():
    ctx = FakeCtx(settings={"target_language": "de"})
    got = pkg.glue.settings(ctx)
    assert got["target_language"] == "de" and got["capture_backend"] == "auto" and got["speak"] is False


# -- dashboard/plugin_api.py ------------------------------------------------------------

def _load_api():
    share_engine("hermeneus_dashboard_package")
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_hermeneus_test",
                                                  ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(data_dir, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    module = _load_api()
    rig = make_rig()
    plugin = module._commands.Plugin(None, owner="gateway", settings=lambda: {"voice_id": ""},
                                     data_dir=lambda: data_dir, build=rig.build,
                                     list_sources=lambda: ([SOURCE], []))
    monkeypatch.setattr(module, "plugin", plugin)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermeneus")
    with TestClient(app) as client:
        yield client, module, plugin, rig
    plugin.unload()


def test_api_idle_status_lines_and_doctor_shape(api, monkeypatch):
    client, module, _plugin, _rig = api
    status = client.get("/api/plugins/hermeneus/status").json()
    assert status["state"] == "idle" and status["type"] == "status"
    assert any(l["code"] == "tr" for l in status["languages"])
    page = client.get("/api/plugins/hermeneus/lines").json()
    assert page["reset"] is True and isinstance(page["seq"], int) and page["events"][0]["type"] == "status"

    from hermeneus_engine.doctor import Check, DoctorReport
    report = DoctorReport([Check("macOS", True, "macOS 26"), Check(pkg.glue.PLUGIN_NAME, False, "x", "do y")])
    monkeypatch.setattr(module._doctor, "run", lambda **kw: report)
    monkeypatch.setattr(module._glue, "aux_config", lambda: {})
    checks = client.get("/api/plugins/hermeneus/doctor").json()["checks"]
    assert checks[0] == {"id": "macos", "label": "macOS", "state": "ok", "detail": "macOS 26"}
    assert checks[1]["state"] == "fail" and checks[1]["fix"] == "do y"


def test_api_start_lines_stop(api):
    client, _module, plugin, rig = api
    body = {"source": {"backend": "virtual-device", "source_id": "BlackHole 2ch"}, "target_language": "tr",
            "speak": False, "output_device": None}
    started = client.post("/api/plugins/hermeneus/start", json=body)
    assert started.status_code == 200 and started.json()["state"] in ("starting", "live")
    busy = client.post("/api/plugins/hermeneus/start", json=body)
    assert busy.status_code == 409 and "already running" in busy.json()["detail"]

    from hermeneus_engine.contracts import CommittedTranscript
    rig.stt._queue._loop.call_soon_threadsafe(rig.stt.emit, CommittedTranscript("hello", segment_index=0))
    bus = plugin.controller.local_bus
    for _ in range(200):
        if any(l.translation for l in bus.lines()):
            break
        time.sleep(0.01)
    page = client.get("/api/plugins/hermeneus/lines", params={"after": 0}).json()
    assert [e["line"]["translation"] for e in page["events"] if e["type"] == "line"][-1] == "<hello>"
    later = client.get("/api/plugins/hermeneus/lines", params={"after": page["seq"]}).json()
    assert later["reset"] is False and later["events"] == []

    stopped = client.post("/api/plugins/hermeneus/stop").json()
    assert stopped["state"] == "idle"


def test_api_start_errors_are_4xx_with_a_message(api):
    client, *_ = api
    r = client.post("/api/plugins/hermeneus/start", json={"source": {"backend": "virtual-device",
                                                                     "source_id": "BlackHole 2ch"}})
    assert r.status_code == 400 and "--to" in r.json()["detail"]
    r = client.post("/api/plugins/hermeneus/start", json={
        "source": {"backend": "virtual-device", "source_id": "BlackHole 2ch"}, "target_language": "af",
        "speak": True, "output_device": "AirPods"})
    assert r.status_code == 400 and "Afrikaans" in r.json()["detail"]


def test_api_events_socket_says_hello_then_heartbeats_while_idle(api, monkeypatch):
    client, module, plugin, _rig = api
    original = plugin.controller.subscribe
    monkeypatch.setattr(plugin.controller, "subscribe", lambda after=None: original(after, heartbeat_s=0.05))
    with client.websocket_connect("/api/plugins/hermeneus/events") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["session"]["state"] == "idle" and "seq" in hello
        beat = ws.receive_json()
        assert beat["type"] == "status" and beat["state"] == "idle"


def test_api_relays_a_session_owned_by_another_process(api, data_dir):
    client, *_ = api

    async def scenario():
        rt = SessionRuntime(data_dir, options(), make_rig().build, owner="cli")
        await rt.start()
        await wait_for(lambda: rt.bus.status()["state"] == "live")
        status = await asyncio.to_thread(lambda: client.get("/api/plugins/hermeneus/status").json())
        await rt.close()
        return status

    assert asyncio.run(scenario())["state"] == "live"


def test_api_overlay_routes(api):
    client, module, plugin, _rig = api
    assert client.get("/api/plugins/hermeneus/overlay").json() == {"visible": False, "pid": None}
    assert client.post("/api/plugins/hermeneus/overlay", json={"visible": "yes"}).status_code == 400
    # The test client is not on 127.0.0.1, so it is a remote Desktop: the overlay is refused, not opened there.
    answer = client.post("/api/plugins/hermeneus/overlay", json={"visible": True})
    assert answer.status_code == 409 and "chat platform" in answer.json()["detail"]
    assert client.post("/api/plugins/hermeneus/overlay", json={"visible": False}).json()["visible"] is False
