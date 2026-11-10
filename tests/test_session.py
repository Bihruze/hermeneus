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
    rig.stt.emit(SttStatus(SttState.FAILED, message="The API key was rejected."))
    await asyncio.wait_for(rt.session.wait_closed(), 2)
    states = []
    while (e := await sub.get(timeout=0.2)) is not None:
        if e["type"] == "status":
            states.append((e["state"], e["message"]))
    assert states[-1] == ("failed", "The API key was rejected.")
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
    assert rt.bus.status() == {**rt.bus.status(), "state": "failed", "message": "BlackHole 2ch disappeared"}
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
