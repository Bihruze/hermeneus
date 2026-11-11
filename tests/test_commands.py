import argparse
import asyncio
import io
import json
import os
import signal
import sys
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
    with pytest.raises(UsageError, match="does not support Afrikaans"):
        resolve(Choices(to="af", source="BlackHole", speak=True, device="Headphones"), {}, [SOURCE])


def test_voice_order_is_flag_then_setting_then_default():
    from hermeneus_engine.tts.voices import DEFAULT_VOICE_ID
    base = dict(to="tr", source="BlackHole", speak=True, device="Headphones")
    assert resolve(Choices(**base), {}, [SOURCE]).voice_id == DEFAULT_VOICE_ID
    assert resolve(Choices(**base), {"voice_id": "  "}, [SOURCE]).voice_id == DEFAULT_VOICE_ID
    assert resolve(Choices(**base), {"voice_id": "cfg"}, [SOURCE]).voice_id == "cfg"
    assert resolve(Choices(voice="flag", **base), {"voice_id": "cfg"}, [SOURCE]).voice_id == "flag"
    assert resolve(Choices(to="tr", source="BlackHole"), {"voice_id": "cfg"}, [SOURCE]).voice_id is None


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
    rig.stt.emit(SttStatus(SttState.FAILED, message="Your ElevenLabs quota is used up.", error_type="quota_exceeded"))
    assert await asyncio.wait_for(task, 3) == 1
    events = [json.loads(l) for l in out.getvalue().splitlines()]
    assert {e["type"] for e in events} >= {"line", "status"}
    assert events[-1]["state"] == "failed" and events[-1]["message"].startswith("Speech to text: the ElevenLabs quota")


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


def test_cli_numbers_speakers_by_first_appearance_and_follows_relabels():
    out, err = io.StringIO(), io.StringIO()
    p = commands._Printer(False, out, err)
    def line(i, spk):
        return {"type": "line", "seq": i, "line": {"line_id": str(i), "state": "translated", "translation": f"t{i}",
                                                   "speaker_id": spk}}
    p(line(1, "s1:speaker_3"))
    p(line(2, "s1:speaker_0"))
    p({"type": "relabel", "seq": 3, "from": "s1:speaker_3", "to": "s2:speaker_0"})
    p(line(4, "s2:speaker_0"))
    p(line(5, "s1:speaker_7"))
    assert out.getvalue().splitlines() == ["[Speaker 1] t1", "[Speaker 2] t2", "[Speaker 1] t4", "[Speaker 3] t5"]
    assert "s1:" not in out.getvalue() + err.getvalue()


def test_llm_errors_become_plain_reasons_with_the_fix():
    glue = pkg.glue
    credit = RuntimeError("Error code: 404 - {'message': \"Model requires available credits.\", "
                          "'code': 'insufficient_credits_for_paid_model'}")
    assert glue.describe_llm_error(credit, "nous") == (
        "Translation failed: the model provider reports insufficient credits (nous). "
        "Add credits with that provider, or pick another model with: "
        "hermes model (Configure auxiliary models > Live translation)")
    InstallError = type("InstallError", (RuntimeError,), {})
    err = InstallError("venv: installation of extra 'anthropic' declined")
    err.cause = "installation of extra 'anthropic' declined"
    assert glue.describe_llm_error(err) == ("Translation failed: the translation model needs Hermes' 'anthropic' "
                                            "feature. Run: hermes pm install --extra anthropic")
    assert "rate limiting" in glue.describe_llm_error(RuntimeError("429 Too Many Requests"), "openrouter")


async def test_llm_call_turns_provider_errors_into_translation_errors():
    from hermeneus_engine.contracts import TranslationError

    class Failing:
        async def acomplete(self, **kw):
            raise RuntimeError("401 Unauthorized")
    call = pkg.glue.make_llm_call(Failing(), provider="openai")
    with pytest.raises(TranslationError, match="rejected the credentials"):
        await call([{"role": "user", "content": "x"}])


# -- set and voices ------------------------------------------------------------------

async def test_slash_set_changes_a_running_session_and_refuses_without_one(data_dir):
    from pipeline_fakes import SOURCE2
    plugin, rig = make_plugin(data_dir, sources=(SOURCE, SOURCE2))
    assert (await plugin.slash("set --to de")).startswith("Not changed: No translation session is running.")
    await plugin.slash("start --to tr --source BlackHole")
    await wait_for(lambda: plugin.controller.local_bus.status()["state"] == "live")
    text = await plugin.slash("set --to de --speak on --device Speakers --voice v7")
    assert text == ("Changed: subtitles now go into de from the next sentence. Applying spoken translation in "
                    "the background; /translate status shows the result.")
    bus = plugin.controller.local_bus
    await wait_for(lambda: bus.status()["mode"] == "both")
    status = await plugin.slash("status")
    assert status == ("Translation is live from BlackHole 2ch into de. Spoken translation is on "
                      "(voice v7, output Speakers).")
    assert (await plugin.slash("set --mode voice")) == "Changed: mode is voice only (subtitles hidden)."
    assert (await plugin.slash("set --source 'All system'")).startswith("Applying source")
    await wait_for(lambda: bus.status()["source"]["source_id"] == "system")
    assert (await plugin.slash("set --speak maybe")).startswith("Not changed: argument --speak")
    assert (await plugin.slash("set")).startswith("Not changed: nothing to change.")
    assert (await plugin.slash("set --source Nowhere")).startswith("Not changed: no audio source called 'Nowhere'")
    await plugin.slash("stop")


def test_cli_set_and_voices(data_dir, monkeypatch):
    from hermeneus_engine.tts.voice_catalog import VoiceCatalog
    plugin, rig = make_plugin(data_dir)
    parser = argparse.ArgumentParser()
    commands.setup_cli(parser)
    out, err = io.StringIO(), io.StringIO()
    args = parser.parse_args(["set", "--to", "de"])
    assert commands.cmd_set(plugin, args, out=out, err=err) == 1
    assert err.getvalue().startswith("hermeneus: not changed: No translation session is running.")
    args = parser.parse_args(["set", "--speak", "off", "--json"])
    out = io.StringIO()
    commands.cmd_set(plugin, args, out=out, err=err)
    assert json.loads(out.getvalue())["ok"] is False

    body = {"voices": [{"voice_id": "OZ0L6eISlOejga3XjDFt", "name": "Talia", "category": "premade",
                        "verified_languages": [{"language": "tr", "model_id": "eleven_flash_v2_5"}]}]}
    plugin.voice_catalog = VoiceCatalog(lambda: "k", fetch=lambda *a: body)
    out = io.StringIO()
    assert commands.cmd_voices(plugin, parser.parse_args(["voices"]), out=out, err=err) == 0
    assert out.getvalue().splitlines() == ["Voices on this ElevenLabs account (pass one with --voice):",
                                           "  Talia (default)  OZ0L6eISlOejga3XjDFt  [premade]  tr"]
    plugin.voice_catalog = VoiceCatalog(lambda: None)
    err = io.StringIO()
    assert commands.cmd_voices(plugin, parser.parse_args(["voices"]), out=out, err=err) == 1
    assert err.getvalue().startswith("hermeneus: Voices: no ElevenLabs API key is set.")


def test_start_body_mode_voice_hides_subtitles():
    opts = resolve(Choices.from_mapping({"mode": "voice", "target_language": "tr", "output_device": "Speakers",
                                         "source": "BlackHole"}), {}, [SOURCE])
    assert (opts.speak, opts.subtitles, opts.mode) == (True, False, "voice")
    with pytest.raises(UsageError, match="mode must be"):
        resolve(Choices(to="tr", source="BlackHole", mode="loud"), {}, [SOURCE])


def test_overlay_commands_refuse_cleanly_without_a_session(data_dir):
    plugin, _rig = make_plugin(data_dir)
    assert asyncio.run(plugin.slash("overlay")) == "The overlay is closed."
    assert "no translation session" in asyncio.run(plugin.slash("overlay show", platform="cli"))
    assert "chat platform (telegram)" in asyncio.run(plugin.slash("overlay show", platform="telegram"))
    assert asyncio.run(plugin.slash("overlay sideways")) == commands.SLASH_USAGE
    out, err = io.StringIO(), io.StringIO()
    args = argparse.Namespace(overlay_action="show", json=False)
    assert asyncio.run(commands.cmd_overlay(plugin, args, out=out, err=err)) == 1
    assert "no translation session" in err.getvalue()
    args = argparse.Namespace(overlay_action="status", json=True)
    assert asyncio.run(commands.cmd_overlay(plugin, args, out=out, err=err)) == 0
    assert json.loads(out.getvalue()) == {"visible": False, "pid": None}


def test_install_hint_format_is_pinned():
    # Same command Hermes prints for a missing extra (pm/extras.py:212-214).
    assert pkg.glue._install_hint("anthropic") == "hermes pm install --extra anthropic"


def test_missing_model_extra_checks_the_sdk_module(monkeypatch):
    import types
    glue = pkg.glue
    fake = types.ModuleType("providers")
    profiles = {"anthropic": types.SimpleNamespace(api_mode="anthropic_messages"),
                "bedrock": types.SimpleNamespace(api_mode="bedrock_converse"),
                "openrouter": types.SimpleNamespace(api_mode="chat_completions")}
    fake.get_provider_profile = profiles.get
    monkeypatch.setitem(sys.modules, "providers", fake)
    present = {"anthropic"}
    monkeypatch.setattr(glue, "_importable", lambda module: module in present)
    assert glue.missing_model_extra("anthropic") is None
    assert glue.missing_model_extra("bedrock") == "bedrock"  # boto3 missing
    assert glue.missing_model_extra("openrouter") is None
    assert glue.missing_model_extra("auto") is None
