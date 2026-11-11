"""GET /v2/voices parsing, paging, caching and errors. No network."""

import io
import urllib.error

import pytest

from hermeneus_engine.tts.voice_catalog import VoiceCatalog, VoiceListError, describe_error
from hermeneus_engine.tts.voices import DEFAULT_VOICE_ID


def page(voices, token=None):
    return {"voices": voices, "has_more": token is not None, "next_page_token": token}


def voice(vid, name, *, category="premade", langs=()):
    return {"voice_id": vid, "name": name, "category": category,
            "verified_languages": [{"language": l, "model_id": m, "accent": None} for l, m in langs]}


def test_pages_flash_languages_default_first_and_key_only_in_header():
    calls = []
    pages = [page([voice("z", "Zed", langs=[("en", "eleven_flash_v2_5"), ("en", "eleven_multilingual_v2")])], "t2"),
             page([voice(DEFAULT_VOICE_ID, "Talia", langs=[("tr", "eleven_flash_v2_5"), ("de", "eleven_flash_v2_5")]),
                   voice("a", "Ada", category="cloned"), voice("z", "Zed dup")])]

    def fetch(url, headers, timeout):
        calls.append((url, headers))
        return pages[len(calls) - 1]

    result = VoiceCatalog(lambda: "secret", fetch=fetch).list()
    assert [v.name for v in result.voices] == ["Talia", "Ada", "Zed"]
    talia, ada, zed = result.voices
    assert talia.is_default and talia.flash_languages == ("tr", "de") and zed.flash_languages == ("en",)
    assert ada.category == "cloned" and ada.flash_languages == ()
    assert calls[0][0] == "https://api.elevenlabs.io/v2/voices?page_size=100&include_total_count=false"
    assert "next_page_token=t2" in calls[1][0]
    assert all("secret" not in url and h["xi-api-key"] == "secret" for url, h in calls)


def test_cache_for_sixty_seconds_and_refresh():
    now = [0.0]
    n = []
    catalog = VoiceCatalog(lambda: "k", fetch=lambda *a: n.append(1) or page([voice("a", "A")]),
                           clock=lambda: now[0])
    catalog.list()
    now[0] = 59.0
    catalog.list()
    assert len(n) == 1
    catalog.list(refresh=True)
    now[0] = 200.0
    catalog.list()
    assert len(n) == 3


def http_error(code):
    return urllib.error.HTTPError("https://api.elevenlabs.io/v2/voices", code, "x", {}, io.BytesIO(b""))


@pytest.mark.parametrize("exc, text", [
    (http_error(401), "Voices: ElevenLabs rejected the API key (HTTP 401). Check ELEVENLABS_API_KEY in Hermes "
                      "(see: hermes hermeneus doctor)."),
    (http_error(429), "Voices: ElevenLabs is rate limiting this account (HTTP 429). Try again in a minute."),
    (urllib.error.URLError("nodename nor servname provided"),
     "Voices: could not reach ElevenLabs (nodename nor servname provided). Check the network connection and "
     "try again."),
])
def test_errors_are_plain(exc, text):
    assert describe_error(exc) == text

    def fetch(*a):
        raise exc
    with pytest.raises(VoiceListError) as info:
        VoiceCatalog(lambda: "k", fetch=fetch).list()
    assert str(info.value) == text


def test_no_key_and_bad_shape():
    with pytest.raises(VoiceListError, match="no ElevenLabs API key is set"):
        VoiceCatalog(lambda: "").list()
    with pytest.raises(VoiceListError, match="cannot read"):
        VoiceCatalog(lambda: "k", fetch=lambda *a: {"items": []}).list()
