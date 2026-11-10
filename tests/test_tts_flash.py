import asyncio
import logging
import urllib.parse

import numpy as np
import pytest

from hermeneus_engine.contracts import Speaker
from hermeneus_engine.tts import elevenlabs_flash as fl
from hermeneus_engine.tts.playback import PlaybackQueue

from fake_audio import DEVICES, fake_factory
from fake_elevenlabs import FakeTtsServer

KEY = "sk_test_0123456789abcdef_SECRET"
VOICE = "TestVoiceId0000000000"


def make_speaker(server, *, key=KEY, **cfg):
    q = PlaybackQueue("Test Built-in Speakers", sample_rate=24000, stream_factory=fake_factory,
                      query_devices=lambda: DEVICES)
    config = fl.FlashConfig(voice_id=VOICE, base_url=server.base_url, **cfg)
    return fl.FlashSpeaker(config, lambda: key, q), q


async def wait_for(pred, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


def done(sp, n):
    return lambda: len([m for m in sp.metrics if m.outcome != "pending"]) >= n


def test_language_list_is_the_documented_32():
    assert len(fl.FLASH_V2_5_LANGUAGES) == 32
    assert fl.normalize_language("tr") == "tr"
    assert fl.normalize_language("en-US") == "en"
    assert fl.normalize_language("nb") == "no"
    with pytest.raises(fl.TtsError):
        fl.normalize_language("xx")


def test_config_validation():
    with pytest.raises(ValueError):
        fl.FlashConfig(voice_id="")
    with pytest.raises(ValueError):
        fl.FlashConfig(voice_id=VOICE, chunk_length_schedule=(10,))
    with pytest.raises(ValueError):
        fl.FlashConfig(voice_id=VOICE, inactivity_timeout_s=200)
    with pytest.raises(ValueError):
        fl.FlashConfig(voice_id=VOICE, output_format="mp3_44100_128")


async def test_connection_url_header_and_message_sequence():
    async with FakeTtsServer() as srv:
        sp, q = make_speaker(srv)
        assert isinstance(sp, Speaker)
        await sp.start()
        await sp.speak("Merhaba dünya.", language="tr")
        await wait_for(done(sp, 1))
        await sp.close()

    conn = srv.connections[0]
    path, _, query = conn["path"].partition("?")
    assert path == f"/v1/text-to-speech/{VOICE}/multi-stream-input"
    qs = dict(urllib.parse.parse_qsl(query))
    assert qs == {"model_id": "eleven_flash_v2_5", "output_format": "pcm_24000",
                  "inactivity_timeout": "180", "language_code": "tr"}
    assert conn["headers"]["xi-api-key"] == KEY
    assert KEY not in conn["path"]
    msgs = conn["messages"]
    assert msgs[0] == {"context_id": "u1", "text": " ",
                       "generation_config": {"chunk_length_schedule": [50, 120, 160, 290]}}
    assert msgs[1] == {"context_id": "u1", "text": "Merhaba dünya. ", "flush": True}
    assert msgs[2] == {"context_id": "u1", "close_context": True}
    assert msgs[3] == {"close_socket": True}
    assert all("xi-api-key" not in m and "xi_api_key" not in m for m in msgs)


async def test_audio_decoded_in_call_order_and_first_audio_stamped():
    async with FakeTtsServer(delays={"u1": 0.15, "u2": 0.0}) as srv:
        sp, q = make_speaker(srv)
        await sp.start("en")
        await sp.speak("first", language="en")
        await sp.speak("second", language="en")
        await wait_for(done(sp, 2))
        out = q._stream.pull(480 * 3 * 2 + 10)
        await sp.close()
    mono = out[:, 0]
    assert (mono[:1440] == 1).all() and (mono[1440:2880] == 2).all() and (mono[2880:] == 0).all()
    assert (out[:, 0] == out[:, 1]).all()
    m1, m2 = sorted(sp.metrics, key=lambda m: m.utterance_id)
    assert m1.outcome == m2.outcome == "done"
    assert m1.first_audio_latency_ns >= 150_000_000 > m2.first_audio_latency_ns > 0
    assert sp.connects == 1  # one socket for the session


async def test_odd_byte_split_is_reassembled():
    async with FakeTtsServer(split_odd=True, chunks_per_context=2, samples_per_chunk=100) as srv:
        sp, q = make_speaker(srv)
        await sp.start("en")
        await sp.speak("odd", language="en")
        await wait_for(done(sp, 1))
        out = q._stream.pull(200)
        await sp.close()
    assert (out[:, 0] == 1).all()


async def test_reconnect_resends_segment_without_audio():
    async with FakeTtsServer(drop_after_text=1) as srv:
        sp, q = make_speaker(srv)
        await sp.start("en")
        await sp.speak("hello there", language="en")
        await wait_for(done(sp, 1))
        await sp.speak("after drop", language="en")
        await wait_for(done(sp, 2))
        await sp.close()
    assert len(srv.connections) == 2
    assert [m.outcome for m in sp.metrics] == ["done", "done"]
    texts = [m.get("text") for m in srv.connections[1]["messages"] if m.get("flush")]
    assert texts == ["hello there ", "after drop "]


async def test_idle_close_reconnects_lazily():
    async with FakeTtsServer(idle_close_s=0.1) as srv:
        sp, q = make_speaker(srv)
        await sp.start("en")
        await sp.speak("one", language="en")
        await wait_for(done(sp, 1))
        await wait_for(lambda: not sp._ws_open())
        await sp.speak("two", language="en")
        await wait_for(done(sp, 2))
        await sp.close()
    assert len(srv.connections) == 2
    assert sp.metrics[0].connect_ns == 0 and sp.metrics[1].connect_ns > 0  # paid by the first segment after idle


async def test_language_change_opens_new_socket():
    async with FakeTtsServer() as srv:
        sp, q = make_speaker(srv)
        await sp.speak("hello", language="en")
        await wait_for(done(sp, 1))
        await sp.speak("hallo", language="de")
        await wait_for(done(sp, 2))
        await sp.close()
    langs = [dict(urllib.parse.parse_qsl(c["path"].partition("?")[2]))["language_code"] for c in srv.connections]
    assert langs == ["en", "de"]


async def test_backlog_skips_synthesis():
    async with FakeTtsServer() as srv:
        sp, q = make_speaker(srv)
        await sp.start("en")
        filler = q.open_utterance("filler", q._clock())
        q.feed(filler, np.zeros(24000 * 4, dtype="<i2").tobytes())  # 4 s queued, limit is 3 s
        await sp.speak("too late", language="en")
        await sp.close()
    assert sp.metrics[-1].outcome == "skipped-backlog"
    assert not any(m.get("flush") for m in srv.connections[0]["messages"])


async def test_key_is_redacted_and_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger().setLevel(logging.DEBUG)
    async with FakeTtsServer(reject_status=401) as srv:
        sp, q = make_speaker(srv)
        with pytest.raises(fl.TtsError) as ei:
            await sp.speak("hi", language="en")
        await sp.close()
    assert "HTTP 401" in str(ei.value)
    assert KEY not in str(ei.value)
    assert KEY not in caplog.text
    async with FakeTtsServer() as srv:
        sp, q = make_speaker(srv)
        await sp.speak("hi", language="en")
        await wait_for(done(sp, 1))
        await sp.close()
    assert KEY not in caplog.text
    assert sp._redact(f"boom {KEY} boom") == "boom [redacted] boom"


async def test_missing_key_is_plain_error():
    async with FakeTtsServer() as srv:
        sp, q = make_speaker(srv, key=None)
        with pytest.raises(fl.TtsError, match="No ElevenLabs API key"):
            await sp.speak("hi", language="en")
        await sp.close()
    assert srv.connections == []


async def test_final_timeout_ends_segment(monkeypatch):
    async with FakeTtsServer() as srv:
        async def never_final(ws, cid, closing):
            for m in srv._audio_messages(cid):
                await ws.send(m)
        monkeypatch.setattr(srv, "_generate", never_final)
        sp, q = make_speaker(srv, final_timeout_s=0.2)
        await sp.speak("stuck", language="en")
        await wait_for(done(sp, 1))
        await sp.close()
    assert sp.metrics[0].outcome == "truncated"


async def test_close_cancels_pending_and_stops_playback():
    async with FakeTtsServer(audio_delay_s=5.0) as srv:
        sp, q = make_speaker(srv)
        await sp.speak("never heard", language="en")
        await sp.close()
        await sp.close()
    assert sp.metrics[0].outcome == "cancelled"
    assert not q.running
    with pytest.raises(fl.TtsError):
        await sp.speak("after close", language="en")
