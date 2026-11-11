import json
import argparse
import json
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
    out = asyncio.run(plugin.slash("start --to tr --source BlackHole", platform="cli"))
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
    # A Desktop on this Mac: the gateway sees a loopback peer (dashboard/plugin_api.py _surface).
    with TestClient(app, client=("127.0.0.1", 50123)) as client:
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


def test_api_settings_changes_the_running_session_and_refuses_clearly(api):
    client, _module, plugin, rig = api
    idle = client.post("/api/plugins/hermeneus/settings", json={"target_language": "de"})
    assert idle.status_code == 409 and idle.json()["detail"].startswith("No translation session is running")
    body = {"source": {"backend": "virtual-device", "source_id": "BlackHole 2ch"}, "target_language": "tr"}
    assert client.post("/api/plugins/hermeneus/start", json=body).status_code == 200
    r = client.post("/api/plugins/hermeneus/settings", json={"target_language": "de", "speak": "on",
                                                              "output_device": "Speakers"})
    assert r.status_code == 200
    reply = r.json()
    assert reply["ok"] and reply["accepted"] == {"target_language": "de"} and reply["applying"] == ["speak"]
    for _ in range(200):
        if client.get("/api/plugins/hermeneus/status").json()["mode"] == "both":
            break
        time.sleep(0.01)
    status = client.get("/api/plugins/hermeneus/status").json()
    assert (status["target_language"], status["mode"], status["output_device"]) == ("de", "both", "Speakers")
    bad = client.post("/api/plugins/hermeneus/settings", json={"colour": "red"})
    assert bad.status_code == 400 and "unknown setting 'colour'" in bad.json()["detail"]
    refused = client.post("/api/plugins/hermeneus/settings", json={"target_language": "af"})
    assert refused.status_code == 409 and "does not support Afrikaans" in refused.json()["detail"]
    assert refused.json()["session"]["target_language"] == "de"
    client.post("/api/plugins/hermeneus/stop")


def test_api_voices_lists_the_account_and_reports_errors(api, monkeypatch):
    client, _module, plugin, _rig = api
    from hermeneus_engine.tts.voice_catalog import VoiceCatalog
    body = {"voices": [{"voice_id": "v1", "name": "Ada", "category": "premade",
                        "verified_languages": [{"language": "de", "model_id": "eleven_flash_v2_5"}]}],
            "has_more": False}
    plugin.voice_catalog = VoiceCatalog(lambda: "k", fetch=lambda url, headers, timeout: body)
    r = client.get("/api/plugins/hermeneus/voices").json()
    assert r["voices"] == [{"voice_id": "v1", "name": "Ada", "category": "premade", "flash_languages": ["de"],
                            "is_default": False}]
    plugin.voice_catalog = VoiceCatalog(lambda: None)
    r = client.get("/api/plugins/hermeneus/voices")
    assert r.status_code == 502 and r.json()["detail"].startswith("Voices: no ElevenLabs API key is set.")


def test_api_overlay_routes(api):
    client, module, plugin, _rig = api
    answer = client.get("/api/plugins/hermeneus/overlay").json()
    # The overlay setting defaults to on, so it is armed for the first session.
    assert answer["visible"] is False and answer["armed"] is True and answer["settings"]["max_lines"] == 2
    assert client.post("/api/plugins/hermeneus/overlay", json={"visible": "yes"}).status_code == 400
    # A client that is not on 127.0.0.1 is a remote Desktop: the overlay is refused, not opened there.
    answer = _client(api, "192.168.1.20").post("/api/plugins/hermeneus/overlay", json={"visible": True})
    assert answer.status_code == 409 and "chat platform" in answer.json()["detail"]
    assert client.post("/api/plugins/hermeneus/overlay", json={"visible": False}).json()["visible"] is False


def test_surface_treats_a_proxied_loopback_request_as_remote(api):
    from starlette.requests import Request
    _client, module, _plugin, _rig = api

    def req(host, headers=()):
        return Request({"type": "http", "method": "POST", "path": "/", "client": (host, 50000),
                        "headers": [(k.lower().encode(), v.encode()) for k, v in headers]})

    assert module._surface(req("127.0.0.1")) == "desktop"
    assert module._surface(req("::1")) == "desktop"
    assert module._surface(req("192.168.1.20")) == "remote"
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "X-Forwarded-Host"):
        assert module._surface(req("127.0.0.1", [(header, "203.0.113.7")])) == "remote", header


THEME = {"mode": "dark", "name": "midnight",
         "colors": {"surface": "rgb(22, 27, 34)", "text": "color(srgb 0.9 0.93 0.95 / 0.94)",
                    "text_secondary": "rgba(230, 237, 243, 0.74)", "text_tertiary": "rgba(230, 237, 243, 0.54)",
                    "stroke": "rgba(74, 132, 254, 0.2)",
                    "speakers": ["rgb(0, 83, 253)", "#55a583", "rgb(219, 112, 75)"]},
         "radius_px": 2.4, "font": "'Segoe WPC', -apple-system, sans-serif"}


def test_api_overlay_theme_is_validated_and_stored(api, data_dir):
    client, module, plugin, _rig = api
    bad = client.post("/api/plugins/hermeneus/overlay/theme", json={**THEME, "mode": "sepia"})
    assert bad.status_code == 400 and "mode" in bad.json()["detail"]
    assert client.post("/api/plugins/hermeneus/overlay/theme", json=THEME).status_code == 200
    stored = json.loads((data_dir / "overlay" / "theme.json").read_text())
    assert stored["dark"] is True and stored["source"] == "desktop:midnight" and len(stored["speakers"]) == 3
    assert client.post("/api/plugins/hermeneus/overlay/apply").json()["visible"] is False


# -- F38: the dashboard routes refuse capture for a remote Desktop -------------------------

START_BODY = {"source": {"backend": "virtual-device", "source_id": "BlackHole 2ch"}, "target_language": "tr",
              "speak": False, "output_device": None}


def _client(api, host, headers=None):
    from fastapi.testclient import TestClient
    _client_, module, _plugin, _rig = api
    return TestClient(_client_.app, client=(host, 50124), headers=headers or {})


def test_api_start_from_a_remote_host_is_403_and_captures_nothing(api, data_dir):
    _c, module, plugin, rig = api
    remote = _client(api, "192.168.1.20")
    r = remote.post("/api/plugins/hermeneus/start", json=START_BODY)
    assert r.status_code == 403 and r.json()["detail"] == module._commands.REMOTE_START_REFUSED
    assert lock_holder(data_dir) is None and rig.scheduler is None


def test_api_start_through_a_local_proxy_counts_as_remote(api, data_dir):
    _c, module, _plugin, rig = api
    proxied = _client(api, "127.0.0.1", headers={"X-Forwarded-For": "203.0.113.7"})
    r = proxied.post("/api/plugins/hermeneus/start", json=START_BODY)
    assert r.status_code == 403
    assert lock_holder(data_dir) is None


def test_api_source_change_from_remote_is_403_but_other_settings_pass_the_gate(api, data_dir):
    client, module, plugin, _rig = api
    assert client.post("/api/plugins/hermeneus/start", json=START_BODY).status_code == 200
    remote = _client(api, "10.0.0.5")
    r = remote.post("/api/plugins/hermeneus/settings", json={"source": {"backend": "virtual-device",
                                                                       "source_id": "BlackHole 2ch"}})
    assert r.status_code == 403 and r.json()["detail"] == module._commands.REMOTE_START_REFUSED
    other = remote.post("/api/plugins/hermeneus/settings", json={"target_language": "de"})
    assert other.status_code != 403
    assert remote.post("/api/plugins/hermeneus/stop").json()["state"] == "idle"  # stop works from anywhere


def test_api_remote_start_with_opt_in(api, data_dir, monkeypatch):
    _c, module, plugin, _rig = api
    monkeypatch.setattr(plugin, "_settings", lambda: {"voice_id": "", "allow_remote_start": True})
    remote = _client(api, "192.168.1.20")
    r = remote.post("/api/plugins/hermeneus/start", json=START_BODY)
    assert r.status_code == 200 and r.json()["state"] in ("starting", "live")


# -- first-run setup (W29) ------------------------------------------------------------

@pytest.fixture
def setup_api(api, monkeypatch):
    client, module, plugin, rig = api
    monkeypatch.setattr(module._glue, "get_secret", lambda name: None)
    monkeypatch.setattr(module._glue, "aux_config", lambda: {})
    monkeypatch.setattr(module._glue, "main_config", lambda: {"model": {"default": "big", "provider": "nous"}})
    monkeypatch.setattr(module._glue, "display_language", lambda: "")
    monkeypatch.setattr(module._onboarding, "system_locale", lambda: "C")
    results = []
    check = module._onboarding.AudioCheck(lambda timeout_s: results.pop(0), lambda: plugin.controller.data_dir,
                                          available=lambda: (True, ""), clock=lambda: 5.0)
    monkeypatch.setattr(module, "audio_check", check)
    return client, module, plugin, results


def test_api_setup_reports_each_step_and_never_the_key(setup_api, monkeypatch):
    client, module, _plugin, _results = setup_api
    state = client.get("/api/plugins/hermeneus/setup", params=[("locale", "tr-TR"), ("locale", "en")]).json()
    assert state["complete"] is False
    assert state["key"]["set"] is False and state["key"]["url"].startswith("https://elevenlabs.io/")
    assert state["audio"]["state"] == "unknown" and "Privacy_ScreenCapture" in state["audio"]["settings_url"]
    # No live_translate pin: the main Hermes model translates, a faster one is only suggested.
    assert state["model"] == {"route": "main", "provider": "nous", "model": "big", "suggest_faster": True,
                              "ready": True}
    # No Hermes display language chosen: the system language the Desktop app reports.
    assert state["language"] == {"code": "tr", "from": "system"}
    monkeypatch.setattr(module._glue, "display_language", lambda: "de")
    monkeypatch.setattr(module._glue, "get_secret", lambda name: "sk-not-shown")
    state = client.get("/api/plugins/hermeneus/setup", params={"locale": "tr-TR"}).json()
    assert state["language"] == {"code": "de", "from": "hermes"} and state["key"]["set"] is True
    assert "sk-not-shown" not in json.dumps(state)


def test_api_setup_audio_check_denied_then_retry_then_complete(setup_api, monkeypatch):
    from hermeneus_engine.capture import tap_doctor
    client, module, _plugin, results = setup_api
    monkeypatch.setattr(module._glue, "get_secret", lambda name: "k")
    results.extend([tap_doctor.DoctorResult(False, tap_doctor.PERMISSION_DENIED, "macOS is giving silence"),
                    tap_doctor.DoctorResult(True, tap_doctor.OK, "System audio capture works.")])

    def settle():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = client.get("/api/plugins/hermeneus/setup").json()
            if state["audio"]["state"] != "running":
                return state
            time.sleep(0.02)
        raise AssertionError("the audio check never ended")

    assert client.post("/api/plugins/hermeneus/setup/audio-check").json()["state"] in ("running", "denied")
    state = settle()
    assert state["audio"]["state"] == "denied" and state["complete"] is False
    assert client.post("/api/plugins/hermeneus/setup/audio-check").status_code == 200
    state = settle()
    assert state["audio"]["state"] == "ok" and state["complete"] is True


def test_api_setup_audio_check_and_settings_refused_from_remote_and_while_running(setup_api, monkeypatch):
    client, module, _plugin, results = setup_api
    remote = _client(setup_api[:1] + (None, None, None), "192.168.1.20")
    assert remote.post("/api/plugins/hermeneus/setup/audio-check").status_code == 403
    assert remote.post("/api/plugins/hermeneus/setup/open-privacy-settings").status_code == 403
    assert results == []  # nothing ran
    assert client.post("/api/plugins/hermeneus/start", json=START_BODY).status_code == 200
    r = client.post("/api/plugins/hermeneus/setup/audio-check")
    assert r.status_code == 409 and "stop it first" in r.json()["detail"]
    client.post("/api/plugins/hermeneus/stop")


def test_api_open_privacy_settings_failure_names_the_pane(setup_api, monkeypatch):
    client, module, _plugin, _results = setup_api
    opened = []
    monkeypatch.setattr(module._onboarding, "open_privacy_settings", lambda: opened.append(True))
    assert client.post("/api/plugins/hermeneus/setup/open-privacy-settings").json() == {"ok": True}

    def broken():
        raise RuntimeError("open failed")

    monkeypatch.setattr(module._onboarding, "open_privacy_settings", broken)
    r = client.post("/api/plugins/hermeneus/setup/open-privacy-settings")
    assert r.status_code == 500 and "Screen & System Audio Recording" in r.json()["detail"]
    assert opened == [True]
