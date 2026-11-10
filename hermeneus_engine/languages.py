"""Language tables for Hermeneus.

Provenance (all fetched 2026-11-10 UTC):

* Listening languages (Scribe v3 and Scribe v3 Live): the "Supported languages"
  list at https://elevenlabs.io/docs/capabilities/speech-to-text.md . The page
  says "Scribe v3 and Scribe v2 support 90+ languages, including:" and then
  lists 100 names, each with an ISO 639-3 code. The English names and the
  ISO 639-3 codes below are copied from that list. The models page
  https://elevenlabs.io/docs/overview/models.md links `scribe_v3_live` to the
  same list.
* Speaking languages (`eleven_flash_v2_5`): the models table at
  https://elevenlabs.io/docs/overview/models.md gives "All
  `eleven_multilingual_v2` languages plus: `hu`, `no`, `vi`", and the
  `eleven_multilingual_v2` row lists 29 codes. The same page says Flash v2.5
  "supports 32 languages".
* ISO 639-1 codes: mapped by Hermeneus from the Library of Congress ISO 639-2
  table https://www.loc.gov/standards/iso639-2/ISO-639-2_utf-8.txt . The
  mapping is ours, not ElevenLabs'. Codes with no ISO 639-1 entry there
  (ast, yue, ceb, fil, kea, luo, nso, umb) carry None.

Subtitle targets are the listening list. Translation runs on the user's own
LLM, so translation quality for any language depends on that model.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Language",
    "UnknownLanguageError",
    "LANGUAGES",
    "normalize",
    "get",
    "stt_codes",
    "tts_codes",
    "target_codes",
    "display_name",
]


@dataclass(frozen=True, slots=True)
class Language:
    iso639_3: str
    iso639_1: str | None
    english_name: str
    stt_supported: bool
    tts_flash_v2_5_supported: bool


class UnknownLanguageError(ValueError):
    """Raised when a code does not match any language Hermeneus knows."""


def _L(code3: str, code1: str | None, name: str, tts: bool) -> Language:
    return Language(code3, code1, name, True, tts)


LANGUAGES: tuple[Language, ...] = (
    _L("afr", 'af', "Afrikaans", False),
    _L("amh", 'am', "Amharic", False),
    _L("ara", 'ar', "Arabic", True),
    _L("hye", 'hy', "Armenian", False),
    _L("asm", 'as', "Assamese", False),
    _L("ast", None, "Asturian", False),
    _L("aze", 'az', "Azerbaijani", False),
    _L("bel", 'be', "Belarusian", False),
    _L("ben", 'bn', "Bengali", False),
    _L("bos", 'bs', "Bosnian", False),
    _L("bul", 'bg', "Bulgarian", True),
    _L("mya", 'my', "Burmese", False),
    _L("yue", None, "Cantonese", False),
    _L("cat", 'ca', "Catalan", False),
    _L("ceb", None, "Cebuano", False),
    _L("nya", 'ny', "Chichewa", False),
    _L("hrv", 'hr', "Croatian", True),
    _L("ces", 'cs', "Czech", True),
    _L("dan", 'da', "Danish", True),
    _L("nld", 'nl', "Dutch", True),
    _L("eng", 'en', "English", True),
    _L("est", 'et', "Estonian", False),
    _L("fil", None, "Filipino", True),
    _L("fin", 'fi', "Finnish", True),
    _L("fra", 'fr', "French", True),
    _L("ful", 'ff', "Fulah", False),
    _L("glg", 'gl', "Galician", False),
    _L("lug", 'lg', "Ganda", False),
    _L("kat", 'ka', "Georgian", False),
    _L("deu", 'de', "German", True),
    _L("ell", 'el', "Greek", True),
    _L("guj", 'gu', "Gujarati", False),
    _L("hau", 'ha', "Hausa", False),
    _L("heb", 'he', "Hebrew", False),
    _L("hin", 'hi', "Hindi", True),
    _L("hun", 'hu', "Hungarian", True),
    _L("isl", 'is', "Icelandic", False),
    _L("ibo", 'ig', "Igbo", False),
    _L("ind", 'id', "Indonesian", True),
    _L("gle", 'ga', "Irish", False),
    _L("ita", 'it', "Italian", True),
    _L("jpn", 'ja', "Japanese", True),
    _L("jav", 'jv', "Javanese", False),
    _L("kea", None, "Kabuverdianu", False),
    _L("kan", 'kn', "Kannada", False),
    _L("kaz", 'kk', "Kazakh", False),
    _L("khm", 'km', "Khmer", False),
    _L("kor", 'ko', "Korean", True),
    _L("kur", 'ku', "Kurdish", False),
    _L("kir", 'ky', "Kyrgyz", False),
    _L("lao", 'lo', "Lao", False),
    _L("lav", 'lv', "Latvian", False),
    _L("lin", 'ln', "Lingala", False),
    _L("lit", 'lt', "Lithuanian", False),
    _L("luo", None, "Luo", False),
    _L("ltz", 'lb', "Luxembourgish", False),
    _L("mkd", 'mk', "Macedonian", False),
    _L("msa", 'ms', "Malay", True),
    _L("mal", 'ml', "Malayalam", False),
    _L("mlt", 'mt', "Maltese", False),
    _L("zho", 'zh', "Mandarin Chinese", True),
    _L("mri", 'mi', "Māori", False),
    _L("mar", 'mr', "Marathi", False),
    _L("mon", 'mn', "Mongolian", False),
    _L("nep", 'ne', "Nepali", False),
    _L("nso", None, "Northern Sotho", False),
    _L("nor", 'no', "Norwegian", True),
    _L("oci", 'oc', "Occitan", False),
    _L("ori", 'or', "Odia", False),
    _L("pus", 'ps', "Pashto", False),
    _L("fas", 'fa', "Persian", False),
    _L("pol", 'pl', "Polish", True),
    _L("por", 'pt', "Portuguese", True),
    _L("pan", 'pa', "Punjabi", False),
    _L("ron", 'ro', "Romanian", True),
    _L("rus", 'ru', "Russian", True),
    _L("srp", 'sr', "Serbian", False),
    _L("sna", 'sn', "Shona", False),
    _L("snd", 'sd', "Sindhi", False),
    _L("sin", 'si', "Sinhala", False),
    _L("slk", 'sk', "Slovak", True),
    _L("slv", 'sl', "Slovenian", False),
    _L("som", 'so', "Somali", False),
    _L("spa", 'es', "Spanish", True),
    _L("swa", 'sw', "Swahili", False),
    _L("swe", 'sv', "Swedish", True),
    _L("tam", 'ta', "Tamil", True),
    _L("tgk", 'tg', "Tajik", False),
    _L("tel", 'te', "Telugu", False),
    _L("tha", 'th', "Thai", False),
    _L("tur", 'tr', "Turkish", True),
    _L("ukr", 'uk', "Ukrainian", True),
    _L("umb", None, "Umbundu", False),
    _L("urd", 'ur', "Urdu", False),
    _L("uzb", 'uz', "Uzbek", False),
    _L("vie", 'vi', "Vietnamese", True),
    _L("cym", 'cy', "Welsh", False),
    _L("wol", 'wo', "Wolof", False),
    _L("xho", 'xh', "Xhosa", False),
    _L("zul", 'zu', "Zulu", False),
)

# The speech-to-text page writes Mandarin as `zho` in its supported list and
# as `cmn` in its WER table; both resolve to the listed entry.
_ALIASES: dict[str, str] = {"cmn": "zho"}

_BY_CODE: dict[str, Language] = {}
for _lang in LANGUAGES:
    _BY_CODE[_lang.iso639_3] = _lang
    if _lang.iso639_1:
        _BY_CODE[_lang.iso639_1] = _lang
for _alias, _target in _ALIASES.items():
    _BY_CODE[_alias] = _BY_CODE[_target]


def get(code: str) -> Language:
    """Return the Language for an ISO 639-1, ISO 639-3 or BCP 47 tag."""
    if not isinstance(code, str) or not code.strip():
        raise UnknownLanguageError(f"empty or non-string language code: {code!r}")
    primary = code.strip().replace("_", "-").split("-", 1)[0].lower()
    try:
        return _BY_CODE[primary]
    except KeyError:
        raise UnknownLanguageError(
            f"unknown language code {code!r}: not in the Scribe v3 listening list"
        ) from None


def normalize(code: str) -> str:
    """Return the ISO 639-3 code for `code` (for example tr-TR gives tur)."""
    return get(code).iso639_3


def display_name(code: str) -> str:
    return get(code).english_name


def stt_codes() -> tuple[str, ...]:
    return tuple(l.iso639_3 for l in LANGUAGES if l.stt_supported)


def tts_codes() -> tuple[str, ...]:
    return tuple(l.iso639_3 for l in LANGUAGES if l.tts_flash_v2_5_supported)


def target_codes() -> tuple[str, ...]:
    """Subtitle targets: every listening language (product decision)."""
    return stt_codes()
