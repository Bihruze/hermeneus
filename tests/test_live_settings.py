"""Changes while running: mode, target language, voice, output device, source. Fakes only."""

import asyncio

import pytest

from pipeline_fakes import SOURCE, SOURCE2, FakeCapture, FakeSpeaker, make_rig, options, remove_tmpdir, short_tmpdir, wait_for

from hermeneus_engine.contracts import CommittedTranscript, FeedbackRiskError, LineState
from hermeneus_engine.ipc import SessionClient, SettingsRefused
from hermeneus_engine.runtime import SessionRuntime, socket_path
from hermeneus_engine.session import SettingRefused
from hermeneus_engine.tts.voices import DEFAULT_VOICE_ID


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


async def live(data_dir, rig=None, **opt):
    rig = rig or make_rig()
    rt = SessionRuntime(data_dir, options(**opt), rig.build, owner="test")
    await rt.start()
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    return rt, rig


def settled(rt):
    return lambda: not rt.bus.status()["applying"] and not rt.session._jobs


@pytest.fixture
def emit():
    def _emit(rig, text, index):
        rig.stt.emit(CommittedTranscript(text, segment_index=index))
    return _emit


async def wait_line(rt, text):
    await wait_for(lambda: any(l.original == text and l.state in (LineState.TRANSLATED, LineState.FAILED)
                               for l in rt.bus.lines()))
    return next(l for l in rt.bus.lines() if l.original == text)


# -- status fields --------------------------------------------------------------------

async def test_status_shows_mode_voice_and_device(data_dir):
    rt, rig = await live(data_dir, make_rig(speaker=FakeSpeaker()), speak=True, output_device="Speakers",
                         voice_id="v1")
    status = rt.bus.status()
    assert (status["mode"], status["speak"], status["subtitles"]) == ("both", True, True)
    assert (status["voice_id"], status["output_device"], status["applying"]) == ("v1", "Speakers", [])
    await rt.close()


# -- target language ----------------------------------------------------------------

async def test_target_change_applies_from_the_next_sentence(data_dir, emit):
    rt, rig = await live(data_dir)
    emit(rig, "first", 0)
    first = await wait_line(rt, "first")
    result = rt.session.apply_settings({"target_language": "de"})
    assert result["accepted"] == {"target_language": "de"} and result["applying"] == []
    assert rt.bus.status()["target_language"] == "de"
    emit(rig, "second", 1)
    second = await wait_line(rt, "second")
    lines = {l.original: l for l in rt.bus.lines()}
    assert first.target_language == "tr" and lines["first"].target_language == "tr"
    assert second.target_language == "de"
    assert [l.target_language for l in rig.scheduler.submitted] == ["tr", "de"]
    await rt.close()


async def test_unknown_language_is_refused_and_nothing_changes(data_dir):
    rt, rig = await live(data_dir)
    with pytest.raises(SettingRefused, match="Subtitle language"):
        rt.session.apply_settings({"target_language": "klingon"})
    assert rt.bus.status()["target_language"] == "tr"
    await rt.close()


async def test_speaking_session_refuses_a_language_without_voice(data_dir):
    rt, rig = await live(data_dir, make_rig(speaker=FakeSpeaker()), speak=True, output_device="Speakers")
    with pytest.raises(SettingRefused, match="does not support Afrikaans"):
        rt.session.apply_settings({"target_language": "af"})
    # With speech off in the same request the change is fine.
    result = rt.session.apply_settings({"target_language": "af", "speak": False})
    assert result["accepted"] == {"target_language": "af", "mode": "subtitles"}
    await rt.close()


# -- mode and speech on / off -------------------------------------------------------

async def test_speak_on_starts_a_speaker_without_touching_capture_or_stt(data_dir, emit):
    rt, rig = await live(data_dir)
    result = rt.session.apply_settings({"speak": True, "output_device": "Speakers", "voice_id": "v2"})
    assert result["applying"] == ["speak"] and rt.bus.status()["applying"] == ["speak"]
    await wait_for(settled(rt))
    [speaker] = rig.speakers
    assert (speaker.voice_id, speaker.device, speaker.started_with) == ("v2", "Speakers", "tr")
    status = rt.bus.status()
    assert (status["mode"], status["voice_id"], status["output_device"]) == ("both", "v2", "Speakers")
    assert rig.capture.stopped == 0 and not rig.stt.closed and rig.captures == []
    emit(rig, "hello", 0)
    await wait_for(lambda: speaker.spoken == [("<hello>", "tr")])
    await rt.close()
    assert speaker.closed


async def test_speak_off_stops_playback_at_once_and_subtitles_continue(data_dir, emit):
    speaker = FakeSpeaker()
    rt, rig = await live(data_dir, make_rig(speaker=speaker), speak=True, output_device="Speakers")
    result = rt.session.apply_settings({"speak": False})
    assert result["accepted"] == {"mode": "subtitles"}
    assert rt.bus.status()["speak"] is False
    await wait_for(lambda: speaker.closed)
    emit(rig, "after", 0)
    line = await wait_line(rt, "after")
    assert line.translation == "<after>" and speaker.spoken == []
    assert rig.capture.stopped == 0 and not rig.stt.closed
    await rt.close()


async def test_voice_only_mode_keeps_the_bus_and_hides_subtitles_in_status(data_dir, emit):
    speaker = FakeSpeaker()
    rt, rig = await live(data_dir, make_rig(speaker=speaker), speak=True, output_device="Speakers")
    result = rt.session.apply_settings({"mode": "voice"})
    assert result["accepted"] == {"mode": "voice"} and result["applying"] == []
    status = rt.bus.status()
    assert (status["mode"], status["subtitles"], status["speak"]) == ("voice", False, True)
    emit(rig, "spoken", 0)
    await wait_line(rt, "spoken")  # still on the bus for the overlay and the pane
    await wait_for(lambda: speaker.spoken == [("<spoken>", "tr")])
    await rt.close()


async def test_mode_voice_from_subtitles_turns_speech_on(data_dir):
    rt, rig = await live(data_dir)
    rt.session.apply_settings({"mode": "voice", "output_device": "Speakers"})
    await wait_for(settled(rt))
    assert rt.bus.status()["mode"] == "voice" and len(rig.speakers) == 1
    await rt.close()


async def test_speak_on_without_device_is_refused(data_dir):
    rt, rig = await live(data_dir)
    with pytest.raises(SettingRefused, match="needs an output device"):
        rt.session.apply_settings({"speak": True})
    with pytest.raises(SettingRefused, match="does not fit mode"):
        rt.session.apply_settings({"speak": True, "mode": "subtitles", "output_device": "x"})
    await rt.close()


async def test_speaker_that_cannot_start_leaves_subtitles_mode_and_says_why(data_dir):
    rig = make_rig()
    rig.next_speaker_error = FeedbackRiskError("Speakers feeds the capture source BlackHole 2ch.")
    rt, rig = await live(data_dir, rig)
    rt.session.apply_settings({"speak": True, "output_device": "Speakers"})
    await wait_for(settled(rt))
    status = rt.bus.status()
    assert status["mode"] == "subtitles" and status["speak"] is False
    assert status["message"] == ("Spoken translation not started: Speakers feeds the capture source "
                                 "BlackHole 2ch.")
    assert rig.speakers[0].closed
    await rt.close()


async def test_speech_off_while_it_is_still_starting_wins(data_dir):
    rt, rig = await live(data_dir)
    rt.session.apply_settings({"speak": True, "output_device": "Speakers"})
    rt.session.apply_settings({"speak": False})
    await wait_for(settled(rt))
    assert rt.bus.status()["speak"] is False and rt.session._c.speaker is None
    assert all(s.closed for s in rig.speakers)
    await rt.close()


# -- voice and output device ------------------------------------------------------

async def test_new_voice_starts_a_new_speaker_then_retires_the_old_one(data_dir, emit):
    first = FakeSpeaker("v1", "Speakers")
    rt, rig = await live(data_dir, make_rig(speaker=first), speak=True, output_device="Speakers", voice_id="v1")
    result = rt.session.apply_settings({"voice_id": "v9"})
    assert result["applying"] == ["voice_id"]
    await wait_for(settled(rt))
    [second] = rig.speakers
    assert first.closed and not second.closed and second.voice_id == "v9"
    assert rt.bus.status()["voice_id"] == "v9"
    emit(rig, "next", 0)
    await wait_for(lambda: second.spoken == [("<next>", "tr")])
    await rt.close()


async def test_new_output_device_reopens_playback_and_a_bad_one_keeps_the_old(data_dir):
    first = FakeSpeaker("v1", "Speakers")
    rt, rig = await live(data_dir, make_rig(speaker=first), speak=True, output_device="Speakers", voice_id="v1")
    rig.next_speaker_error = RuntimeError("Output device 'Gone' not found. Available outputs: Speakers")
    rt.session.apply_settings({"output_device": "Gone"})
    await wait_for(settled(rt))
    assert not first.closed and rt.session._c.speaker is first
    assert rt.bus.status()["output_device"] == "Speakers"
    assert "Spoken translation not changed: Output device 'Gone' not found" in rt.bus.status()["message"]
    rig.next_speaker_error = None
    rt.session.apply_settings({"output_device": "Headphones"})
    await wait_for(settled(rt))
    assert first.closed and rt.bus.status()["output_device"] == "Headphones"
    await rt.close()


async def test_bad_voice_id_is_refused(data_dir):
    rt, _ = await live(data_dir)
    with pytest.raises(SettingRefused, match="not an ElevenLabs voice id"):
        rt.session.apply_settings({"voice_id": "a/b", "speak": True, "output_device": "x"})
    await rt.close()


async def test_default_voice_when_none_given(data_dir):
    rt, rig = await live(data_dir)
    rt.session.apply_settings({"speak": True, "output_device": "Speakers"})
    await wait_for(settled(rt))
    assert rig.speakers[0].voice_id == DEFAULT_VOICE_ID
    await rt.close()


# -- source ---------------------------------------------------------------------------

async def test_source_change_keeps_the_stt_session(data_dir):
    rt, rig = await live(data_dir)
    new = FakeCapture()
    rig.next_capture = new
    result = rt.session.apply_settings({"source": {"backend": "process-tap", "source_id": "system",
                                                   "label": SOURCE2.label}})
    assert result["applying"] == ["source"]
    await wait_for(settled(rt))
    assert rig.capture.stopped == 1 and new.started and not rig.stt.closed
    assert rt.bus.status()["source"]["source_id"] == "system"
    new.push(b"\x01\x00" * 160)
    await wait_for(lambda: rig.stt.audio and rig.stt.audio[-1][0] == b"\x01\x00" * 160)
    await rt.close()
    assert new.stopped == 1


async def test_source_that_cannot_start_keeps_the_old_capture(data_dir):
    rt, rig = await live(data_dir)
    rig.next_capture = FakeCapture(fail_start="Zoom is not running or has not opened audio yet.")
    rt.session.apply_settings({"source": SOURCE2})
    await wait_for(settled(rt))
    assert rig.capture.stopped == 0 and rt.bus.status()["source"]["source_id"] == SOURCE.source_id
    assert rt.bus.status()["message"] == ("Source not changed: Zoom is not running or has not opened audio yet. "
                                          "Still capturing BlackHole 2ch.")
    await rt.close()


async def test_source_change_while_speaking_runs_the_feedback_check_first(data_dir):
    rt, rig = await live(data_dir, make_rig(speaker=FakeSpeaker()), speak=True, output_device="Speakers")
    rig.route_error = FeedbackRiskError("Speakers would be captured by All system audio.")
    rt.session.apply_settings({"source": SOURCE2})
    await wait_for(settled(rt))
    assert rig.route_checks == [("Speakers", SOURCE2)] and rig.captures == []
    assert "Source not changed: Speakers would be captured" in rt.bus.status()["message"]
    await rt.close()


async def test_same_source_is_a_no_op(data_dir):
    rt, rig = await live(data_dir)
    result = rt.session.apply_settings({"source": SOURCE})
    assert result == {**result, "accepted": {}, "applying": []}
    await rt.close()


async def test_components_without_factories_refuse_with_restart_message(data_dir):
    rt, rig = await live(data_dir, make_rig(live_changes=False))
    with pytest.raises(SettingRefused, match="start it again with --source"):
        rt.session.apply_settings({"source": SOURCE2})
    with pytest.raises(SettingRefused, match="start it again with --speak"):
        rt.session.apply_settings({"speak": True, "output_device": "x"})
    await rt.close()


# -- socket ------------------------------------------------------------------------------

async def test_set_over_the_socket_answers_at_once(data_dir):
    rt, rig = await live(data_dir)
    client = SessionClient(socket_path(data_dir))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    reply = await client.set({"target_language": "fr", "speak": True, "output_device": "Speakers"})
    assert loop.time() - t0 < 0.5
    assert reply["ok"] and reply["accepted"] == {"target_language": "fr"} and reply["applying"] == ["speak"]
    with pytest.raises(SettingsRefused, match="Unknown setting 'colour'"):
        await client.set({"colour": "red"})
    with pytest.raises(SettingsRefused, match="non-empty"):
        await client.set({})
    await rt.close()


async def test_set_after_the_session_ended_is_refused(data_dir):
    rt, rig = await live(data_dir)
    session = rt.session
    await rt.close()
    with pytest.raises(SettingRefused, match="No translation session is running"):
        session.apply_settings({"target_language": "de"})
