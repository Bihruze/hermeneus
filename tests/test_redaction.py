import pytest

from hermeneus_engine.redaction import KEEP_KEYS, KeyRedactor
from hermeneus_engine.tts import elevenlabs_flash as fl

from fake_elevenlabs import FakeTtsServer
from test_tts_flash import make_speaker, wait_for, done

OLD, NEW = "sk_old_0123456789abcdef", "sk_new_fedcba9876543210"


def test_redactor_keeps_the_last_four_keys():
    r = KeyRedactor()
    keys = [f"sk_key_{i:02d}_abcdefgh" for i in range(KEEP_KEYS + 1)]
    for k in keys:
        r.remember(k)
    r.remember("")
    out = r.redact(" ".join(keys))
    assert out.split() == [keys[0]] + ["[redacted]"] * KEEP_KEYS
    assert r.redact("nothing here") == "nothing here"


async def test_tts_redacts_a_rotated_key_and_its_predecessor():
    current = {"key": OLD}
    async with FakeTtsServer() as srv:
        sp, _q = make_speaker(srv, key=None)
        sp._key_provider = lambda: current["key"]
        await sp.speak("one", language="en")
        await wait_for(done(sp, 1))
        current["key"] = NEW
        await sp._drop_socket()
        await sp.speak("two", language="en")
        await wait_for(done(sp, 2))
        await sp.close()
    assert sp._redact(f"a {OLD} b {NEW}") == "a [redacted] b [redacted]"
