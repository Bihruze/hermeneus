import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from pipeline_fakes import FakeCapture, FakeSpeaker, FakeStt, make_rig, options, remove_tmpdir, short_tmpdir, wait_for

from hermeneus_engine.bus import SubtitleBus
from hermeneus_engine.contracts import (CommittedTranscript, DiarizedTranscript, LineState, PartialTranscript,
                                        SpeakersMerged, SttState, SttStatus, Word)
from hermeneus_engine.ipc import SessionClient, SessionUnavailable
from hermeneus_engine.runtime import (SessionBusy, SessionHost, SessionLock, SessionRuntime, lock_holder,
                                      lock_path, run_dir, socket_path)
from hermeneus_engine.session import BYTES_PER_SECOND, FrameQueue, TranslationSession


@pytest.fixture
def data_dir():
    path = short_tmpdir()
    yield path
    remove_tmpdir(path)


def lines_by_id(bus):
    return {l.line_id: l for l in bus.lines()}


async def started(data_dir, rig=None, **opt):
    rig = rig or make_rig()
    rt = SessionRuntime(data_dir, options(**opt), rig.build, owner="test")
    await rt.start()
    return rt, rig


async def test_start_live_status_and_stop(data_dir):
    rt, rig = await started(data_dir)
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    status = rt.bus.status()
    assert status["source"]["source_id"] == "BlackHole 2ch" and status["target_language"] == "tr"
    assert lock_holder(data_dir)["pid"] == os.getpid()
    assert socket_path(data_dir).exists()
    await rt.close()
    assert rt.bus.status()["state"] == "idle"
    assert rig.capture.stopped == 1 and rig.stt.closed and rig.scheduler.closed
    assert lock_holder(data_dir) is None and not socket_path(data_dir).exists()


async def test_partial_commit_translate_flow_updates_one_line(data_dir):
    rt, rig = await started(data_dir)
    rig.stt.emit(PartialTranscript("hel", words=(Word("hel", speaker_id="s1:speaker_0"),)))
    rig.stt.emit(PartialTranscript("hello"))
    rig.stt.emit(CommittedTranscript("hello there", segment_index=0, language_code="eng", received_ns=5))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    [line] = rt.bus.lines()
    assert line.original == "hello there" and line.translation == "<hello there>"
    assert line.speaker_id == "s1:speaker_0"
    assert line.source_language == "en"  # Scribe's eng goes out as ISO 639-1
    assert rt.bus.status()["detected_language"] == "en"
    assert rig.scheduler.submitted[0].timings_ns["commit"] == 5
    await rt.close()


async def test_translation_failure_keeps_original_text(data_dir):
    def broken(_text):
        raise RuntimeError("model unavailable")

    rt, rig = await started(data_dir, make_rig(translate=broken))
    rig.stt.emit(CommittedTranscript("good morning", segment_index=0))
    await wait_for(lambda: any(l.state == LineState.FAILED for l in rt.bus.lines()))
    [line] = rt.bus.lines()
    assert line.original == "good morning" and line.translation == "" and "model unavailable" in line.error
    assert rt.bus.status()["state"] == "live"
    await rt.close()


async def test_relabel_reaches_bus_stored_lines_and_scheduler(data_dir):
    rt, rig = await started(data_dir)
    sub = rt.bus.subscribe()
    rig.stt.emit(CommittedTranscript("one", segment_index=0))
    rig.stt.emit(DiarizedTranscript(0, "one", (Word("one", speaker_id="s1:speaker_1"),)))
    await wait_for(lambda: rt.bus.lines() and rt.bus.lines()[0].speaker_id == "s1:speaker_1")
    rig.stt.emit(SpeakersMerged("s1:speaker_1", "s1:speaker_0"))
    await wait_for(lambda: rig.scheduler.relabels)
    assert rig.scheduler.relabels == [("s1:speaker_1", "s1:speaker_0")]
    assert all(l.speaker_id == "s1:speaker_0" for l in rt.bus.lines())
    events = []
    while (e := await sub.get(timeout=0.2)) is not None:
        events.append(e)
    assert {"type": "relabel", "from": "s1:speaker_1", "to": "s1:speaker_0"}.items() <= \
        next(e for e in events if e["type"] == "relabel").items()
    await rt.close()


async def test_stt_failed_status_reaches_consumers_and_releases_lock(data_dir):
    rt, rig = await started(data_dir)
    sub = rt.bus.subscribe()
    rig.stt.emit(SttStatus(SttState.FAILED, message="The API key was rejected.", error_type="auth_error"))
    await asyncio.wait_for(rt.session.wait_closed(), 2)
    states = []
    while (e := await sub.get(timeout=0.2)) is not None:
        if e["type"] == "status":
            states.append((e["state"], e["message"]))
    assert states[-1][0] == "failed"
    assert states[-1][1].startswith("Speech to text: ElevenLabs rejected the API key. Check ELEVENLABS_API_KEY")
    await rt.wait()
    assert lock_holder(data_dir) is None


async def test_capture_error_from_backend_thread_fails_session(data_dir):
    rt, rig = await started(data_dir)
    from hermeneus_engine.contracts import CaptureError
    import threading
    t = threading.Thread(target=rig.capture_error, args=(CaptureError("BlackHole 2ch disappeared"),))
    t.start()
    t.join()
    await asyncio.wait_for(rt.session.wait_closed(), 2)
    assert rt.bus.status()["state"] == "failed"
    assert rt.bus.status()["message"] == ("BlackHole 2ch disappeared. Start translation again, or pick another "
                                          "source (see: hermes hermeneus devices).")
    await rt.close()


async def test_capture_start_error_is_raised_and_cleans_up(data_dir):
    rig = make_rig(capture=FakeCapture(fail_start="Microphone access was denied for Terminal."))
    rt = SessionRuntime(data_dir, options(), rig.build, owner="test")
    with pytest.raises(Exception, match="Microphone access was denied"):
        await rt.start()
    assert lock_holder(data_dir) is None and not socket_path(data_dir).exists()
    assert rig.stt.closed


async def test_audio_reaches_stt_in_order(data_dir):
    rt, rig = await started(data_dir)
    for i in range(5):
        rig.capture.push(bytes([i]) * 320, ns=i)
    await wait_for(lambda: len(rig.stt.audio) == 5)
    assert [ns for _pcm, ns in rig.stt.audio] == [0, 1, 2, 3, 4]
    await rt.close()


async def test_frame_queue_drops_oldest_and_counts():
    loop = asyncio.get_running_loop()
    got = []
    q = FrameQueue(loop, max_bytes=BYTES_PER_SECOND // 10, consumer=lambda pcm, ns: got.append(ns))  # 100 ms
    chunk = b"\0" * (BYTES_PER_SECOND // 50)  # 20 ms
    for i in range(20):  # the loop does not run between these puts
        q.put(chunk, i)
    await asyncio.sleep(0)
    assert got == list(range(15, 20))
    assert (q.chunks_in, q.chunks_dropped) == (20, 15)
    assert q.bytes_dropped == 15 * len(chunk)
    q.close()
    q.put(chunk, 99)
    await asyncio.sleep(0)
    assert got == list(range(15, 20))


async def test_backpressure_drops_show_in_status_metrics(data_dir):
    rt, rig = await started(data_dir, max_queued_seconds=0.02)
    chunk = b"\0" * (BYTES_PER_SECOND // 100)  # 10 ms
    for i in range(10):  # the loop does not run between these puts
        rt.session._frames.put(chunk, i)
    await wait_for(lambda: len(rig.stt.audio) >= 2)
    metrics = rt.session.metrics()
    assert metrics["audio_chunks_dropped"] == 8 and metrics["audio_dropped_ms"] == 80
    await rt.close()


async def test_spoken_translation_follows_translated_lines(data_dir):
    speaker = FakeSpeaker()
    rt, rig = await started(data_dir, make_rig(speaker=speaker), speak=True, output_device="Headphones",
                            voice_id="v1")
    rig.stt.emit(CommittedTranscript("hi", segment_index=0))
    await wait_for(lambda: speaker.spoken)
    assert speaker.spoken == [("<hi>", "tr")] and speaker.started_with == "tr"
    await rt.close()
    assert speaker.closed


async def test_second_start_refused_while_one_runs(data_dir):
    rt, _ = await started(data_dir)
    other = SessionRuntime(data_dir, options(), make_rig().build, owner="test")
    with pytest.raises(SessionBusy) as info:
        await other.start()
    assert info.value.info["pid"] == os.getpid()
    await rt.close()


def test_second_start_refused_through_session_host(data_dir):
    host = SessionHost(data_dir, owner="test")
    status = host.start(options(), make_rig().build)
    assert status["state"] in ("starting", "live")
    other = SessionHost(data_dir, owner="test")
    with pytest.raises(SessionBusy):
        other.start(options(), make_rig().build)
    assert host.stop() is True
    assert lock_holder(data_dir) is None


def test_stale_lock_from_killed_owner_is_recovered(data_dir):
    run_dir(data_dir)
    code = ("import fcntl, os, socket, sys, time\n"
            f"fd = os.open({str(lock_path(data_dir))!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "os.write(fd, b'{\"pid\": %d}' % os.getpid())\n"
            "s = socket.socket(socket.AF_UNIX); "
            f"s.bind({str(socket_path(data_dir))!r})\n"
            "print('ready', flush=True); time.sleep(60)\n")
    child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ready"
        holder = lock_holder(data_dir)
        assert holder["pid"] == child.pid and holder["pid_alive"] is True
        lock = SessionLock(data_dir)
        with pytest.raises(SessionBusy):
            lock.acquire({"pid": os.getpid()})
        child.send_signal(signal.SIGKILL)
        child.wait(5)
    finally:
        child.kill()
    assert socket_path(data_dir).exists()  # the dead owner left its socket file
    assert lock_holder(data_dir) is None

    async def start_again():
        rt, _ = await started(data_dir)
        assert lock_holder(data_dir)["pid"] == os.getpid()
        await rt.close()
    asyncio.run(start_again())


async def test_second_client_attaches_over_the_socket(data_dir):
    rt, rig = await started(data_dir)
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    rig.stt.emit(CommittedTranscript("first", segment_index=0))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    client = SessionClient(socket_path(data_dir))
    assert (await client.status())["state"] == "live"
    page = await client.lines(None)
    assert page["reset"] is True and page["seq"] == rt.bus.last_seq
    assert [e["line"]["translation"] for e in page["events"] if e["type"] == "line"] == ["<first>"]

    stream = client.subscribe(after=0)
    hello = await stream.__anext__()
    assert hello["type"] == "hello" and hello["seq"] == rt.bus.last_seq
    first = await stream.__anext__()
    rig.stt.emit(CommittedTranscript("second", segment_index=1))
    seen = [first]
    while not any(e.get("type") == "line" and e["line"]["translation"] == "<second>" for e in seen):
        seen.append(await asyncio.wait_for(stream.__anext__(), 2))
    await stream.aclose()

    await client.stop()
    await asyncio.wait_for(rt.wait(), 3)
    assert lock_holder(data_dir) is None
    with pytest.raises(SessionUnavailable):
        await client.status()


async def test_stop_finishes_within_two_seconds_even_with_slow_parts(data_dir):
    rig = make_rig(capture=FakeCapture(stop_delay=5.0), stt=FakeStt(close_delay=5.0))
    rt, _ = await started(data_dir, rig)
    t0 = time.monotonic()
    await rt.close()
    assert time.monotonic() - t0 < 2.0
    assert rt.bus.status()["state"] == "idle"


async def test_session_needs_start_before_wait():
    s = TranslationSession(options(), make_rig().build(options(), None), SubtitleBus())
    with pytest.raises(AssertionError):
        await s.wait_closed()


async def test_speaker_status_message_reaches_session_status():
    speaker = FakeSpeaker()
    opts = options(speak=True, output_device="Headphones", voice_id="v1")
    bus = SubtitleBus()
    s = TranslationSession(opts, make_rig(speaker=speaker).build(opts, None), bus, metrics_interval_s=0.01)
    await s.start()
    await wait_for(lambda: bus.status()["state"] == "live")
    speaker.status_message = "Voice v1 is not available on this ElevenLabs account; using the default voice Talia."
    await wait_for(lambda: "default voice Talia" in (bus.status().get("message") or ""))
    await s.stop()


# -- early translation at sentence boundaries --------------------------------------------

async def test_stable_sentence_is_translated_before_the_commit_and_nothing_twice(data_dir):
    rt, rig = await started(data_dir)
    rig.stt.emit(PartialTranscript("Heavy rain flooded the roads. Schools", received_ns=1))
    rig.stt.emit(PartialTranscript("Heavy rain flooded the roads. Schools closed", received_ns=2))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    first = [l for l in rt.bus.lines() if l.state == LineState.TRANSLATED]
    assert [l.original for l in first] == ["Heavy rain flooded the roads."]
    assert first[0].timings_ns["sentence_released"] == 2
    partial = [l for l in rt.bus.lines() if l.state == LineState.PARTIAL]
    assert [l.original for l in partial] == ["Schools closed"]
    rig.stt.emit(CommittedTranscript("Heavy rain flooded several roads. Schools closed for a second day. "
                                     "Officials expect more rain", segment_index=0))
    await wait_for(lambda: sum(l.state == LineState.TRANSLATED for l in rt.bus.lines()) == 3)
    assert [l.original for l in rt.bus.lines()] == [
        "Heavy rain flooded the roads.", "Schools closed for a second day.", "Officials expect more rain"]
    assert [l.original for l in rig.scheduler.submitted] == [l.original for l in rt.bus.lines()]
    assert rt.session.metrics()["sentences_released_early"] == 1
    # the schools line kept the partial line's id: one line per sentence, in order
    assert partial[0].line_id == rt.bus.lines()[1].line_id
    rig.stt.emit(DiarizedTranscript(0, "", tuple(
        [Word(w, speaker_id="s1:speaker_0") for w in "Heavy rain flooded several roads.".split()]
        + [Word(w, speaker_id="s1:speaker_1") for w in "Schools closed for a second day. Officials expect more rain".split()])))
    await wait_for(lambda: rt.bus.lines()[1].speaker_id == "s1:speaker_1")
    assert [l.speaker_id for l in rt.bus.lines()] == ["s1:speaker_0", "s1:speaker_1", "s1:speaker_1"]
    await rt.close()


async def test_splitting_off_keeps_one_line_per_segment(data_dir):
    rt, rig = await started(data_dir, sentence_stable_ms=-1)
    rig.stt.emit(PartialTranscript("One two three four. Five six", received_ns=1))
    rig.stt.emit(PartialTranscript("One two three four. Five six seven", received_ns=2))
    rig.stt.emit(CommittedTranscript("One two three four. Five six seven.", segment_index=0))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    assert [l.original for l in rt.bus.lines()] == ["One two three four. Five six seven."]
    await rt.close()


async def test_model_failure_reason_reaches_the_status_once(data_dir):
    def broke(_text):
        raise RuntimeError("Translation failed: the model provider reports insufficient credits (nous).")

    rt, rig = await started(data_dir, make_rig(translate=broke))
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    sub = rt.bus.subscribe()
    for i in range(3):
        rig.stt.emit(CommittedTranscript(f"line number {i}", segment_index=i))
    await wait_for(lambda: rt.session.stats["lines_failed"] == 3)
    messages = []
    while (e := await sub.get(timeout=0.2)) is not None:
        if e["type"] == "status":
            messages.append(e["message"])
    assert messages.count("Translation failed: the model provider reports insufficient credits (nous).") == 1
    await rt.close()


async def test_last_sentence_emitted_during_stt_close_reaches_the_bus(data_dir):
    """F17: the STT client commits trailing audio inside close(); that line must not be lost."""
    rt, rig = await started(data_dir)
    await wait_for(lambda: rt.bus.status()["state"] == "live")
    rig.stt.emit(PartialTranscript("and finally"))
    await wait_for(lambda: any(l.original == "and finally" for l in rt.bus.lines()))
    rig.stt.on_close = [CommittedTranscript("and finally the end.", segment_index=3)]
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await rt.close()
    assert loop.time() - t0 < 1.5
    assert rig.stt.close_timeout == 1.2
    [line] = [l for l in rt.bus.lines() if l.original]
    assert line.original == "and finally the end." and line.state == LineState.FAILED
    assert line.error == "Not translated: the session stopped while this sentence arrived."
    assert rt.bus.status()["state"] == "idle"


async def test_stt_loop_is_cancelled_at_the_stop_deadline(data_dir):
    rt, rig = await started(data_dir, make_rig(stt=FakeStt(close_delay=5.0)))
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await rt.close()
    assert loop.time() - t0 < 2.0
    await asyncio.sleep(0)
    assert rt.session._stt_task.cancelled()


async def test_lines_of_a_session_owned_elsewhere_stay_after_it_ends(data_dir):
    """F27: the pane asks the gateway for /lines?after=<last seq> after a CLI session ended."""
    from hermeneus_engine.control import Controller
    rt, rig = await started(data_dir)
    gateway = Controller(lambda: data_dir, owner="gateway")
    rig.stt.emit(CommittedTranscript("hello there", segment_index=0))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    page = await gateway.lines(None)
    assert page["reset"] is True and any(e["type"] == "line" for e in page["events"])
    seen = page["seq"]
    await rt.close()
    after = await gateway.lines(seen)
    assert after["reset"] is False  # the pane keeps the lines it has
    assert [e["type"] for e in after["events"]] == ["status"] and after["events"][0]["state"] == "idle"
    fresh = await gateway.lines(None)
    assert fresh["reset"] is True  # a pane that has nothing gets an empty snapshot


# -- pause release and stage timings (E17, E4, E5) -----------------------------------------

def _timed(text, start_s, step_s=0.3, speaker=None):
    words = []
    t = start_s
    for w in text.split():
        words.append(Word(w, start=t, end=t + step_s * 0.8, speaker_id=speaker, type="word"))
        t += step_s
    return tuple(words)


async def test_finished_sentence_is_released_at_a_pause_and_lines_carry_timings(data_dir):
    rt, rig = await started(data_dir, sentence_pause_ms=300)
    base = time.monotonic_ns() - 1_800_000_000
    for i in range(90):  # 1.8 s of audio already captured (under the 2 s queue bound)
        rt.session._frames.put(b"\0" * 640, base + i * 20_000_000)
        if i % 20 == 0:
            await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    words = _timed("The council approved the plan.", 0.5, step_s=0.2)
    rig.stt.emit(PartialTranscript("The council approved the plan.", words, received_ns=time.monotonic_ns()))
    await wait_for(lambda: any(l.state == LineState.TRANSLATED for l in rt.bus.lines()))
    [line] = rt.bus.lines()
    assert line.original == "The council approved the plan." and rt.session.stats["sentences_released_at_pause"] == 1
    rig.stt.emit(CommittedTranscript("The council approved the plan.", segment_index=0))
    rig.stt.emit(DiarizedTranscript(0, "", words))
    await wait_for(lambda: "speech_end" in rt.bus.lines()[0].timings_ns)
    events, _ = rt.bus.events_after(None)
    timings = [e for e in events if e["type"] == "line"][0]["timings"]
    assert set(timings) >= {"capture_ms", "speech_end_ms", "committed_ms", "released_ms", "translated_ms"}
    # capture of the first word: 0.5 s after the first sample
    assert abs(timings["capture_ms"] - ((base + 500_000_000) - rt.session._started_ns) / 1e6) < 1.0
    summary = rt.session.metrics()["latency_ms"]
    assert summary["end_of_sentence_to_subtitle"]["count"] == 1 and "p50" in summary["end_of_sentence_to_subtitle"]
    assert len(rig.stt.audio) == 90  # the stream clock wrapper still hands every chunk to the STT client
    await rt.close()


async def test_word_times_are_not_mapped_after_a_new_scribe_session(data_dir):
    rt, rig = await started(data_dir)
    rig.stt.stats = type("S", (), {"sessions_opened": 1})()
    rt.session._frames.put(b"\0" * 640, time.monotonic_ns())
    await asyncio.sleep(0.01)
    assert rt.session._word_capture_ns(0.01) is not None
    rig.stt.stats.sessions_opened = 2
    rt.session._frames.put(b"\0" * 640, time.monotonic_ns())
    await asyncio.sleep(0.01)
    assert rt.session._word_capture_ns(0.01) is None
    await rt.close()


async def test_first_spoken_audio_reaches_line_timings_and_status(data_dir):
    speaker = FakeSpeaker()
    rig = make_rig(speaker=speaker)
    rt, rig = await started(data_dir, rig, speak=True, output_device="Headphones", voice_id="v1")
    rig.stt.emit(CommittedTranscript("hello there friend", segment_index=0))
    await wait_for(lambda: speaker.spoken)
    line_id = rt.bus.lines()[0].line_id
    now = time.monotonic_ns()
    metrics = type("M", (), {"utterance_id": line_id, "first_audio_ns": now, "first_audio_latency_ns": 250_000_000})()
    rig.build_components.speech_metrics(metrics)
    await wait_for(lambda: "tts_first_audio" in rt.bus.lines()[0].timings_ns)
    summary = rt.session.metrics()["latency_ms"]
    assert summary["tts_request_to_first_audio"] == {"count": 1, "p50": 250, "p95": 250}
    assert summary["translated_to_first_tts_audio"]["count"] == 1
    await rt.close()
