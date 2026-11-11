"""A1 end-to-end findings E1, E2, E6, E7, E14, E16: each test failed before its fix."""

import asyncio

import pytest

from pipeline_fakes import FakeCapture, FakeSpeaker, make_rig, options, wait_for

from hermeneus_engine import messages, session as session_mod
from hermeneus_engine.bus import SubtitleBus
from hermeneus_engine.contracts import CommittedTranscript, LineState, SttState, SttStatus, TranslationError
from hermeneus_engine.session import TranslationSession

SPEECH = b"\x10\x00" * 1600
SILENCE = bytes(3200)


async def live_session(rig, **opt):
    bus = SubtitleBus()
    s = TranslationSession(options(**opt), rig.build(options(**opt), lambda e: None), bus, metrics_interval_s=0.01)
    await s.start()
    await wait_for(lambda: bus.status()["state"] == "live")
    return s, bus


# -- E1: a silent source is not a network failure -----------------------------------------

async def test_e1_no_audio_at_all_says_so_and_scribe_idle_close_is_not_a_network_loss(monkeypatch):
    monkeypatch.setattr(session_mod, "NO_AUDIO_S", 0.05)
    rig = make_rig()
    s, bus = await live_session(rig)
    await wait_for(lambda: bus.status()["message"].startswith("No audio from"))
    assert bus.status()["message"] == ("No audio from BlackHole 2ch. Waiting for sound; if something is playing, "
                                       "check that it is not muted.")
    # What A1 saw at 15.5 s with a muted tab: Scribe closed the idle socket.
    rig.stt.emit(SttStatus(SttState.RECONNECTING, "Connection to ElevenLabs dropped (closed)",
                           error_type="connection_closed"))
    rig.stt.emit(SttStatus(SttState.LIVE))
    await asyncio.sleep(0.05)
    assert bus.status()["state"] == "live" and bus.status()["message"].startswith("No audio from")
    assert "connection to ElevenLabs was lost" not in bus.status()["message"]
    rig.capture.push(SPEECH)
    await wait_for(lambda: bus.status()["message"] in ("", "live"))
    await s.stop()


async def test_e1_digital_silence_is_held_back_and_the_first_sound_goes_through_at_once(monkeypatch):
    monkeypatch.setattr(session_mod, "NO_AUDIO_S", 0.05)
    rig = make_rig()
    s, bus = await live_session(rig)
    rig.capture.push(SPEECH)
    for _ in range(40):
        rig.capture.push(SILENCE)
        await asyncio.sleep(0.005)
        if s._gated:
            break
    assert s._gated and bus.status()["message"].startswith("No audio from")
    sent = len(rig.stt.audio)
    for _ in range(5):
        rig.capture.push(SILENCE)
    await asyncio.sleep(0.02)
    assert len(rig.stt.audio) == sent and s.stats["silent_chunks_held"] >= 5
    rig.capture.push(SPEECH, ns=42)
    await wait_for(lambda: rig.stt.audio[-1] == (SPEECH, 42))
    assert not s._gated
    await wait_for(lambda: not bus.status()["message"].startswith("No audio"))
    await s.stop()


async def test_e1_sound_never_triggers_the_message(monkeypatch):
    monkeypatch.setattr(session_mod, "NO_AUDIO_S", 0.05)
    rig = make_rig()
    s, bus = await live_session(rig)
    for _ in range(30):
        rig.capture.push(SPEECH)
        await asyncio.sleep(0.005)
    assert not bus.status()["message"].startswith("No audio") and not s._gated
    await s.stop()


# -- E2: capacity is not a rate limit ------------------------------------------------------

def test_e2_capacity_has_its_own_text():
    assert messages.stt_reconnecting("resource_exhausted: The service is currently at capacity.",
                                     "resource_exhausted") == (
        "Speech to text: ElevenLabs is at capacity right now; retrying.")
    assert "rate limiting" in messages.stt_reconnecting("Scribe reported rate_limited", "rate_limited")
    assert messages.stt_failed("Lost connection ... The service is currently at capacity.", "resource_exhausted") == (
        "Speech to text stopped: ElevenLabs stayed at capacity and did not take the stream. Start translation "
        "again in a minute.")


# -- E16: the slow-model message ends with the first success -------------------------------

async def test_e16_slow_message_clears_on_the_first_success():
    outcomes = iter([TranslationError("Translation timed out after 6 s."),
                     TranslationError("Translation timed out after 6 s."), None])

    def translate(text):
        out = next(outcomes)
        if out is not None:
            raise out
        return f"<{text}>"

    rig = make_rig(translate=translate)
    s, bus = await live_session(rig)
    for i, text in enumerate(("one", "two")):
        rig.stt.emit(CommittedTranscript(text, segment_index=i))
        await wait_for(lambda: len(bus.lines()) == i + 1 and bus.lines()[-1].state == LineState.FAILED)
    await wait_for(lambda: bus.status()["message"].startswith("Translation: the model is slower"))
    rig.stt.emit(CommittedTranscript("three", segment_index=2))
    await wait_for(lambda: bus.lines()[-1].state == LineState.TRANSLATED)
    assert not bus.status()["message"].startswith("Translation:")
    await s.stop()


# -- E6: the configured voice applies when speech turns on mid-session ---------------------

async def test_e6_configured_voice_and_device_apply_when_speech_turns_on():
    from pipeline_fakes import SOURCE
    from hermeneus_engine.options import Choices, resolve
    opts = resolve(Choices(to="tr", source="BlackHole"), {"voice_id": "seher-voice", "output_device": "AirPods"},
                   [SOURCE])
    assert (opts.speak, opts.voice_id, opts.output_device) == (False, "seher-voice", "AirPods")
    rig = make_rig()
    bus = SubtitleBus()
    s = TranslationSession(opts, rig.build(opts, lambda e: None), bus, metrics_interval_s=0.01)
    await s.start()
    await wait_for(lambda: bus.status()["state"] == "live")
    assert bus.status()["voice_id"] is None  # shown only while speech is on
    s.apply_settings({"speak": True})
    await wait_for(lambda: bus.status()["speak"] is True)
    assert (rig.speakers[0].voice_id, rig.speakers[0].device) == ("seher-voice", "AirPods")
    await s.stop()


# -- E7: a voice chosen while speech is off is kept and answered; unknown ids are refused -----

async def test_e7_voice_while_speech_is_off_is_kept_for_the_next_start():
    rig = make_rig()
    s, bus = await live_session(rig)
    result = s.apply_settings({"voice_id": "v7", "output_device": "Speakers"})
    assert result["accepted"] == {"voice_id": "v7", "output_device": "Speakers"} and result["applying"] == []
    s.apply_settings({"speak": True})
    await wait_for(lambda: bus.status()["speak"] is True)
    assert rig.speakers[0].voice_id == "v7"
    await s.stop()


async def test_e7_slash_set_refuses_a_voice_not_on_the_account(tmp_path):
    from plugin_loader import load_plugin_package
    from pipeline_fakes import SOURCE, remove_tmpdir, short_tmpdir
    from hermeneus_engine.tts.voice_catalog import VoiceCatalog
    commands = load_plugin_package().commands
    data_dir = short_tmpdir()
    try:
        rig = make_rig()
        plugin = commands.Plugin(None, owner="test", settings=lambda: {}, data_dir=lambda: data_dir, build=rig.build,
                                 list_sources=lambda: ([SOURCE], []))
        body = {"voices": [{"voice_id": "v1", "name": "Ada"}]}
        plugin.voice_catalog = VoiceCatalog(lambda: "k", fetch=lambda *a: body)
        await plugin.slash("start --to tr --source BlackHole", platform="cli")
        await wait_for(lambda: plugin.controller.local_bus.status()["state"] == "live")
        # No list fetched yet: nothing to check against, the change goes through.
        assert (await plugin.slash("set --voice v9", platform="cli")) == "Changed: voice v9 is used when speech turns on."
        await asyncio.to_thread(plugin.voice_catalog.list)
        assert (await plugin.slash("set --voice nope", platform="cli")) == (
            "Not changed: voice 'nope' is not on this ElevenLabs account. List the voices with: hermes hermeneus voices")
        assert (await plugin.slash("set --voice v1", platform="cli")) == "Changed: voice v1 is used when speech turns on."
        await plugin.slash("stop", platform="cli")
    finally:
        plugin.unload()
        remove_tmpdir(data_dir)


# -- E14: refusals are sentences; status names the language -------------------------------------

def test_e14_overlay_refusal_is_a_sentence_and_status_names_the_language(tmp_path):
    from plugin_loader import load_plugin_package
    commands = load_plugin_package().commands
    plugin = commands.Plugin(None, owner="test", settings=lambda: {}, data_dir=lambda: tmp_path,
                             build=make_rig().build, list_sources=lambda: ([], []))
    text = asyncio.run(plugin.slash("overlay show", platform="cli"))
    assert text.startswith("The subtitle overlay did not open. No translation session is running. Start one first")
    assert text.endswith("session.")
    assert commands.describe_status({"state": "live", "target_language": "tr"}) == "Translation is live into Turkish."
    assert commands.describe_status({"state": "live", "target_language": "xx-unknown"}).endswith("into xx-unknown.")
