"""The failure matrix (docs/failures.md): every failure triggered with fakes, with its exact user text."""

import asyncio

import pytest

from pipeline_fakes import FakeCapture, FakeSpeaker, FakeStt, make_rig, options, wait_for
from plugin_loader import load_plugin_package

from hermeneus_engine import messages
from hermeneus_engine.bus import SubtitleBus
from hermeneus_engine.capture.process_tap import permission_message
from hermeneus_engine.contracts import CommittedTranscript, LineState, SttState, SttStatus, TranslationError
from hermeneus_engine.session import TranslationSession
from hermeneus_engine.tts.elevenlabs_flash import TtsError
from hermeneus_engine.tts.voices import unavailable_message

pkg = load_plugin_package()
KEYS = "https://elevenlabs.io/app/settings/api-keys"
PLAN = "https://elevenlabs.io/app/subscription"


async def session_with(rig, **opt):
    bus = SubtitleBus()
    session = TranslationSession(options(**opt), rig.build(options(**opt), lambda e: None), bus,
                                 metrics_interval_s=0.01)
    await session.start()
    await wait_for(lambda: bus.status()["state"] == "live")
    return session, bus


async def ended_with(session, bus):
    await asyncio.wait_for(session.wait_closed(), 2)
    return bus.status()["state"], bus.status()["message"]


async def live_message(bus, predicate):
    await wait_for(lambda: predicate(bus.status()["message"]))
    return bus.status()["message"]


# 1. no key ---------------------------------------------------------------------------

async def test_no_key_speech_to_text():
    rig = make_rig()
    session, bus = await session_with(rig)
    rig.stt.emit(SttStatus(SttState.FAILED, "ELEVENLABS_API_KEY is not set.", error_type="missing_key"))
    assert await ended_with(session, bus) == ("failed", (
        "Speech to text: no ElevenLabs API key is set. Add ELEVENLABS_API_KEY to Hermes "
        "(see: hermes hermeneus doctor), then start translation again."))


async def test_no_key_spoken_translation_keeps_subtitles():
    speaker = FakeSpeaker(fail_speak=TtsError("No ElevenLabs API key is configured for spoken translation."))
    rig = make_rig(speaker=speaker)
    session, bus = await session_with(rig, speak=True, output_device="Speakers")
    rig.stt.emit(CommittedTranscript("hello", segment_index=0))
    msg = await live_message(bus, lambda m: m.startswith("Spoken translation"))
    assert msg == ("Spoken translation: no ElevenLabs API key is set. Add ELEVENLABS_API_KEY to Hermes "
                   "(subtitles continue).")
    assert bus.status()["state"] == "live" and bus.lines()[0].translation == "<hello>"
    await session.stop()


# 2. invalid key --------------------------------------------------------------------------

@pytest.mark.parametrize("etype", ["auth_error", "http_401"])
async def test_invalid_key_speech_to_text(etype):
    rig = make_rig()
    session, bus = await session_with(rig)
    rig.stt.emit(SttStatus(SttState.FAILED, "ElevenLabs rejected the API key.", error_type=etype))
    assert await ended_with(session, bus) == ("failed", (
        "Speech to text: ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in Hermes "
        f"(a new key is at {KEYS}), then start translation again."))


def test_invalid_key_spoken_translation_and_no_stt_permission():
    assert messages.speak_error("ElevenLabs rejected the API key for spoken translation (HTTP 401).") == (
        "Spoken translation: ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY in Hermes "
        "(subtitles continue).")
    assert messages.stt_failed("The ElevenLabs API key does not have Speech to Text permission.", "http_403") == (
        "Speech to text: the ElevenLabs API key has no Speech to Text permission. Allow it for the key at "
        f"{KEYS}, then start translation again.")


# 3. no System Audio Recording permission / zero audio (W02 watchdog) -------------------------

async def test_no_permission_zero_audio_ends_with_the_watchdog_text():
    capture = FakeCapture()
    rig = make_rig(capture=capture)
    session, bus = await session_with(rig)
    capture.failure = permission_message("Hermes")
    state, msg = await ended_with(session, bus)
    assert state == "failed"
    assert msg == ("macOS is giving Hermeneus silence instead of audio. Open System Settings > Privacy & Security "
                   "> Screen & System Audio Recording, turn on 'Hermes' under System Audio Recording, then start "
                   "translation again.")


# 4. no credits on the model provider ------------------------------------------------------------

async def test_no_credits_is_a_line_error_and_a_status_message():
    credit = RuntimeError("Error code: 402 - {'code': 'insufficient_credits'}")
    text = pkg.glue.describe_llm_error(credit, "nous")

    def broke(_):
        raise TranslationError(text)

    rig = make_rig(translate=broke)
    session, bus = await session_with(rig)
    rig.stt.emit(CommittedTranscript("good morning", segment_index=0))
    msg = await live_message(bus, lambda m: m.startswith("Translation failed"))
    assert msg == text == ("Translation failed: the model provider reports insufficient credits (nous). "
                           "Add credits with that provider, or pick another model with: "
                           "hermes model (Configure auxiliary models > Live translation)")
    assert bus.lines()[0].state == LineState.FAILED and bus.lines()[0].error == text
    await session.stop()


# 5. model missing a Hermes extra ------------------------------------------------------------------

def test_model_missing_extra_at_start_and_in_a_call(monkeypatch):
    glue = pkg.glue
    monkeypatch.setattr(glue, "_aux_provider", lambda: "anthropic")
    monkeypatch.setattr(glue, "missing_model_extra", lambda provider: "anthropic")
    with pytest.raises(TranslationError) as info:
        glue.model_preflight()
    assert str(info.value) == ("the translation model (anthropic) needs Hermes' 'anthropic' feature, which is "
                               "not installed. Run: hermes pm install --extra anthropic")
    checks = pkg.commands.model_checks(False)
    assert checks[0].fix == "run: hermes pm install --extra anthropic"


# 6. model slow: over the lag bound -----------------------------------------------------------------

async def test_model_slow_after_two_late_lines():
    errors = iter(["Skipped to keep up: the translation would have been more than 8 s behind.",
                   "Translation timed out after 6 s."])

    def slow(_):
        raise TranslationError(next(errors))

    rig = make_rig(translate=slow)
    session, bus = await session_with(rig)
    rig.stt.emit(CommittedTranscript("one", segment_index=0))
    await wait_for(lambda: bus.lines() and bus.lines()[0].state == LineState.FAILED)
    assert not bus.status()["message"].startswith("Translation")  # one late line is noise
    rig.stt.emit(CommittedTranscript("two", segment_index=1))
    msg = await live_message(bus, lambda m: m.startswith("Translation:"))
    assert msg == ("Translation: the model is slower than the speech; 2 lines in the last 20 s came late and "
                   "were skipped or timed out. Pick a faster model with: "
                   "hermes model (Configure auxiliary models > Live translation).")
    await session.stop()


# 7. ElevenLabs quota or rate limited ----------------------------------------------------------------

async def test_quota_used_up_speech_to_text():
    rig = make_rig()
    session, bus = await session_with(rig)
    rig.stt.emit(SttStatus(SttState.FAILED, "Your ElevenLabs quota is used up.", error_type="quota_exceeded"))
    assert await ended_with(session, bus) == ("failed", (
        "Speech to text: the ElevenLabs quota is used up, so transcription stopped. Add credits or change "
        f"the plan at {PLAN}, then start translation again."))


async def test_rate_limited_speech_to_text_reconnects():
    rig = make_rig()
    session, bus = await session_with(rig)
    rig.stt.emit(SttStatus(SttState.RECONNECTING, "Scribe reported rate_limited", error_type="rate_limited"))
    await wait_for(lambda: bus.status()["state"] == "reconnecting")
    assert bus.status()["message"] == ("Speech to text: ElevenLabs is rate limiting this account; reconnecting. "
                                       "Subtitles resume when it accepts audio again.")
    await session.stop()


async def test_quota_and_rate_limit_spoken_translation_from_the_server():
    speaker = FakeSpeaker()
    rig = make_rig(speaker=speaker)
    session, bus = await session_with(rig, speak=True, output_device="Speakers")
    speaker.last_error = "This request exceeds your quota of 10000. You have 3 credits remaining."
    msg = await live_message(bus, lambda m: m.startswith("Spoken translation"))
    assert msg == ("Spoken translation: the ElevenLabs quota is used up. Add credits or change the plan at "
                   f"{PLAN}, or turn speech off (subtitles continue).")
    assert messages.speak_error("ElevenLabs is rate limiting spoken translation or the quota is used up "
                                "(HTTP 429).").startswith("Spoken translation: the ElevenLabs quota is used up.")
    assert messages.speak_error("Too many concurrent requests") == (
        "Spoken translation: ElevenLabs is rate limiting this account; some sentences are not spoken. "
        "Speech resumes when it allows more (subtitles continue).")
    await session.stop()


# 8. voice unavailable ---------------------------------------------------------------------------------

async def test_voice_unavailable_falls_back_and_says_so():
    speaker = FakeSpeaker()
    rig = make_rig(speaker=speaker)
    session, bus = await session_with(rig, speak=True, output_device="Speakers")
    speaker.status_message = unavailable_message("abc123")
    msg = await live_message(bus, lambda m: "abc123" in m)
    assert msg == ("Voice abc123 is not available on this ElevenLabs account; using the default voice Talia.")
    await session.stop()


# 9. network lost: STT reconnecting, then failed ------------------------------------------------------

async def test_network_lost_reconnecting_then_failed():
    rig = make_rig()
    session, bus = await session_with(rig)
    rig.stt.emit(SttStatus(SttState.RECONNECTING, "Could not reach ElevenLabs: [Errno 8] nodename nor servname",
                           error_type="connect_failed"))
    await wait_for(lambda: bus.status()["state"] == "reconnecting")
    assert bus.status()["message"] == ("Speech to text: the connection to ElevenLabs was lost; reconnecting. "
                                       "Check the network connection if this does not clear.")
    rig.stt.emit(SttStatus(SttState.FAILED, "Lost connection to ElevenLabs after 6 attempts. Last error: Could "
                           "not reach ElevenLabs.", error_type="connect_failed"))
    assert await ended_with(session, bus) == ("failed", (
        "Speech to text stopped: the connection to ElevenLabs was lost and did not come back (Lost connection "
        "to ElevenLabs after 6 attempts. Last error: Could not reach ElevenLabs.). Check the network "
        "connection, then start translation again."))


# 10. output device gone -------------------------------------------------------------------------------

async def test_output_device_gone_turns_speech_off_and_subtitles_continue():
    speaker = FakeSpeaker()
    rig = make_rig(speaker=speaker)
    session, bus = await session_with(rig, speak=True, output_device="AirPods")
    speaker.unhealthy = "Output device 'AirPods' stopped playing"
    msg = await live_message(bus, lambda m: m.startswith("Spoken translation stopped"))
    assert msg == ("Spoken translation stopped: the output device 'AirPods' is no longer available. Pick another "
                   "output device and turn speech on again (subtitles continue).")
    await wait_for(lambda: speaker.closed)
    assert bus.status()["speak"] is False and bus.status()["state"] == "live"
    rig.stt.emit(CommittedTranscript("still here", segment_index=0))
    await wait_for(lambda: bus.lines() and bus.lines()[0].state == LineState.TRANSLATED)
    await session.stop()


def test_playback_health_reports_a_stream_that_stopped_by_itself():
    from hermeneus_engine.tts.playback import PlaybackQueue

    class Stream:
        active = True

        def __init__(self, **kw):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def close(self):
            pass

    q = PlaybackQueue("AirPods", stream_factory=Stream,
                      query_devices=lambda: [{"name": "AirPods", "max_output_channels": 2}])
    q.start()
    assert q.health() == ""
    q._stream.active = False
    assert q.health() == "Output device 'AirPods' stopped playing; it may have been unplugged or switched off."
    q.stop()
    assert q.health() == ""


# 11. source app quit ---------------------------------------------------------------------------------

async def test_source_app_quit():
    capture = FakeCapture()
    rig = make_rig(capture=capture)
    session, bus = await session_with(rig)
    capture.failure = "Capture from zoom.us stopped unexpectedly."
    assert await ended_with(session, bus) == ("failed", (
        "Capture from zoom.us stopped unexpectedly, for example because the app quit. Start translation "
        "again, or pick another source (see: hermes hermeneus devices)."))


def test_source_stalled_keeps_the_watchdog_text():
    text = "Capture from zoom.us stopped receiving audio. Stop and start translation again."
    assert messages.capture_stopped(text, "zoom.us") == text
