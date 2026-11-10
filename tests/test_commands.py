import argparse
import asyncio
import io
import json
import os
import signal
import time

import pytest

from pipeline_fakes import SOURCE, make_rig, options, remove_tmpdir, short_tmpdir, wait_for
from plugin_loader import load_plugin_package

from hermeneus_engine.contracts import CaptureSource, CommittedTranscript
from hermeneus_engine.options import Choices, UsageError, parse_slash_start, resolve
from hermeneus_engine.runtime import SessionRuntime, lock_holder

pkg = load_plugin_package()
commands = pkg.commands


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def make_plugin(data_dir, rig=None, settings=None, sources=(SOURCE,)):
    rig = rig or make_rig()
    plugin = commands.Plugin(None, owner="test", settings=lambda: dict(settings or {}),
                             data_dir=lambda: data_dir, build=rig.build,
                             list_sources=lambda: (list(sources), []))
    return plugin, rig


def run_args(**kw):
    ns = argparse.Namespace(to=None, source=None, speak=False, device=None, voice=None, json=False)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


# -- options --------------------------------------------------------------------

def test_missing_choices_name_the_flag():
    with pytest.raises(UsageError, match="--to"):
        resolve(Choices(), {}, [SOURCE])
    two = [SOURCE, CaptureSource("virtual-device", "VB-Cable", "VB-Cable")]
    with pytest.raises(UsageError, match="--source"):
        resolve(Choices(to="tr"), {}, two)
    with pytest.raises(UsageError, match="--device"):
        resolve(Choices(to="tr", source="BlackHole", speak=True), {}, [SOURCE])
    with pytest.raises(UsageError, match="voice_id"):
        resolve(Choices(to="tr", source="BlackHole", speak=True, device="Headphones"), {}, [SOURCE])


def test_flags_win_over_settings_and_codes_go_out_as_iso639_1():
    opts = resolve(Choices(to="tur", source="blackhole"), {"target_language": "de", "context_segments": 2}, [SOURCE])
    assert opts.target_language == "tr" and opts.source == SOURCE and opts.speak is False
    assert opts.context_segments == 2
    opts = resolve(Choices(source={"backend": "virtual-device", "source_id": "BlackHole 2ch"}, speak=True,
                           device="Headphones"), {"target_language": "de", "voice_id": "v1"}, [SOURCE])
    assert opts.speak and opts.voice_id == "v1" and opts.output_device == "Headphones"


def test_auto_backend_prefers_the_process_tap_system_source():
    tap = CaptureSource("process-tap", "system", "All system audio except Hermeneus")
    app = CaptureSource("process-tap", "app:com.google.Chrome", "Google Chrome")
    assert resolve(Choices(to="tr"), {}, [SOURCE, tap, app]).source == tap
    assert resolve(Choices(to="tr"), {"capture_backend": "virtual-device"}, [SOURCE, tap]).source == SOURCE
    assert resolve(Choices(to="tr", source="chrome"), {}, [SOURCE, tap, app]).source == app
    with pytest.raises(UsageError, match="capture_backend"):
        resolve(Choices(to="tr"), {"capture_backend": "bluetooth"}, [SOURCE])


def test_unknown_language_and_unknown_source_are_usage_errors():
    with pytest.raises(UsageError, match="--to"):
        resolve(Choices(to="xx-nope", source="BlackHole"), {}, [SOURCE])
    with pytest.raises(UsageError, match="Available sources: BlackHole 2ch"):
        resolve(Choices(to="tr", source="Soundflower"), {}, [SOURCE])


def test_slash_parser_never_exits():
    with pytest.raises(UsageError):
        parse_slash_start(["--bogus"])
    assert parse_slash_start(["--to", "tr", "--speak"]).speak is True


# -- slash command ----------------------------------------------------------------

async def test_slash_start_status_stop(data_dir):
    plugin, rig = make_plugin(data_dir)
    out = await plugin.slash("start --to tr --source BlackHole")
    assert out.startswith("Translation is") and "into tr" in out
    await wait_for(lambda: plugin.controller.local_bus.status()["state"] == "live")
    assert "live" in await plugin.slash("status")
    assert "already running" in await plugin.slash("start --to tr --source BlackHole")
    assert await plugin.slash("stop") == "Translation is off."
    assert lock_holder(data_dir) is None


async def test_slash_start_without_choices_explains_flags(data_dir):
    plugin, _ = make_plugin(data_dir)
    out = await plugin.slash("start")
    assert "Could not start translation" in out and "--to" in out
    assert "usage: /translate" in await plugin.slash("bogus")
    assert "takes no arguments" in await plugin.slash("stop now")


async def test_slash_sees_a_session_owned_by_another_owner(data_dir):
    rt = SessionRuntime(data_dir, options(), make_rig().build, owner="cli")
    await rt.start()
    plugin, _ = make_plugin(data_dir)
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    assert "live" in await plugin.slash("status")
    stopping = asyncio.create_task(plugin.slash("stop"))
    await asyncio.wait_for(rt.wait(), 3)
    assert (await stopping) == "Translation is off."


# -- CLI run -----------------------------------------------------------------------

async def test_run_prints_translated_lines_and_stops_on_sigint(data_dir):
    plugin, rig = make_plugin(data_dir)
    out, err = io.StringIO(), io.StringIO()
    task = asyncio.create_task(commands.cmd_run(plugin, run_args(to="tr", source="BlackHole"), out=out, err=err))
    await wait_for(lambda: rig.scheduler is not None and lock_holder(data_dir))
    await asyncio.sleep(0.05)
    rig.stt.emit(CommittedTranscript("hello", segment_index=0))
    await wait_for(lambda: "<hello>" in out.getvalue())
    t0 = time.monotonic()
    os.kill(os.getpid(), signal.SIGINT)
    code = await asyncio.wait_for(task, 3)
    assert time.monotonic() - t0 < 2.0
    assert code == 0
    assert lock_holder(data_dir) is None
    assert "Translation is live" in err.getvalue()


async def test_run_json_prints_the_event_schema(data_dir):
    plugin, rig = make_plugin(data_dir)
    out = io.StringIO()
    task = asyncio.create_task(commands.cmd_run(plugin, run_args(to="tr", source="BlackHole", json=True),
                                                out=out, err=io.StringIO(), install_signals=False))
    await wait_for(lambda: rig.scheduler is not None)
    rig.stt.emit(CommittedTranscript("hi", segment_index=0))
    await wait_for(lambda: '"translated"' in out.getvalue())
    from hermeneus_engine.contracts import SttState, SttStatus
    rig.stt.emit(SttStatus(SttState.FAILED, message="quota exceeded"))
    assert await asyncio.wait_for(task, 3) == 1
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    assert {e["type"] for e in events} >= {"line", "status"}
    assert events[-1]["state"] == "failed" and events[-1]["message"] == "quota exceeded"


async def test_run_attaches_to_a_running_session_and_detaches_on_signal(data_dir):
    rig = make_rig()
    rt = SessionRuntime(data_dir, options(), rig.build, owner="gateway")
    await rt.start()
    plugin, _ = make_plugin(data_dir)
    out, err = io.StringIO(), io.StringIO()
    task = asyncio.create_task(commands.cmd_run(plugin, run_args(to="de"), out=out, err=err))
    await wait_for(lambda: rt.bus.subscriber_count == 1)
    rig.stt.emit(CommittedTranscript("bonjour", segment_index=0))
    await wait_for(lambda: "<bonjour>" in out.getvalue())
    os.kill(os.getpid(), signal.SIGINT)
    assert await asyncio.wait_for(task, 2) == 0
    assert "start flags ignored" in err.getvalue()
    assert rt.session.state == "live"  # detaching leaves the owner's session running
    await rt.close()


async def test_run_without_choices_exits_2_naming_the_flag(data_dir):
    plugin, _ = make_plugin(data_dir)
    err = io.StringIO()
    code = await commands.cmd_run(plugin, run_args(), out=io.StringIO(), err=err, install_signals=False)
    assert code == 2 and "--to" in err.getvalue()


def test_devices_output_lists_sources_and_safe_outputs():
    out = io.StringIO()
    payload = {"sources": [{"backend": "virtual-device", "source_id": "BlackHole 2ch", "label": "BlackHole 2ch",
                            "detail": "2 in"}], "outputs": [{"device_id": "AirPods", "label": "AirPods"}],
               "unavailable": [], "outputs_note": ""}
    assert commands.cmd_devices(argparse.Namespace(json=False), out=out, payload=lambda: payload) == 0
    assert "BlackHole 2ch  [virtual-device]" in out.getvalue() and "AirPods" in out.getvalue()


def test_devices_payload_never_offers_a_risky_output(monkeypatch):
    from hermeneus_engine import components
    from hermeneus_engine.contracts import CaptureSource, OutputDevice
    other = CaptureSource("virtual-device", "VB-Cable", "VB-Cable")
    monkeypatch.setattr(components, "list_sources", lambda: ([SOURCE, other], []))
    safe = {"BlackHole 2ch": [OutputDevice("AirPods", "AirPods"), OutputDevice("VB-Cable", "VB-Cable")],
            "VB-Cable": [OutputDevice("AirPods", "AirPods")]}
    monkeypatch.setattr(components, "safe_output_devices", lambda s: safe[s.source_id])
    pkg_components = __import__(f"{pkg.__name__}.hermeneus_engine.components", fromlist=["x"])
    monkeypatch.setattr(pkg_components, "list_sources", components.list_sources)
    monkeypatch.setattr(pkg_components, "safe_output_devices", components.safe_output_devices)
    assert [o["device_id"] for o in commands.devices_payload()["outputs"]] == ["AirPods"]
    chosen = commands.devices_payload("virtual-device", "BlackHole 2ch")
    assert [o["device_id"] for o in chosen["outputs"]] == ["AirPods", "VB-Cable"]
