import pytest

from hermeneus_engine import languages as L


def test_counts_match_documented_lists():
    assert len(L.stt_codes()) == 100
    assert len(L.tts_codes()) == 32
    assert len(set(L.stt_codes())) == 100
    assert L.target_codes() == L.stt_codes()


def test_tts_is_subset_of_stt():
    assert set(L.tts_codes()) <= set(L.stt_codes())


def test_flash_v2_5_codes_from_models_page():
    documented = (
        "en ja zh de hi fr ko pt it es id nl tr fil pl sv bg ro ar cs el fi hr "
        "ms sk da ta uk ru hu no vi"
    ).split()
    assert sorted(L.normalize(c) for c in documented) == sorted(L.tts_codes())


@pytest.mark.parametrize("lang", L.LANGUAGES, ids=lambda l: l.iso639_3)
def test_round_trip(lang):
    assert L.normalize(lang.iso639_3) == lang.iso639_3
    if lang.iso639_1:
        assert L.normalize(lang.iso639_1) == lang.iso639_3
        assert L.get(lang.iso639_3).iso639_1 == lang.iso639_1


@pytest.mark.parametrize(
    "tag,expected",
    [("tr", "tur"), ("tr-TR", "tur"), ("en-US", "eng"), ("zh-Hans", "zho"),
     ("pt-BR", "por"), ("EN_gb", "eng"), ("cmn", "zho"), ("fil", "fil")],
)
def test_bcp47(tag, expected):
    assert L.normalize(tag) == expected


def test_tr_and_en_present():
    assert L.display_name("tr") == "Turkish"
    assert L.display_name("en") == "English"
    assert L.get("tr").tts_flash_v2_5_supported
    assert L.get("en").tts_flash_v2_5_supported


@pytest.mark.parametrize("bad", ["xx", "yor", "", "  ", None, "klingon"])
def test_unknown_raises(bad):
    with pytest.raises(L.UnknownLanguageError):
        L.normalize(bad)


def test_frozen():
    with pytest.raises(Exception):
        L.LANGUAGES[0].iso639_3 = "zzz"
