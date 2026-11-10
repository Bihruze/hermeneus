"""Target languages the translator can name in its prompt.

PROVISIONAL TABLE. Workstream W12 verifies which languages the configured
models translate well and replaces ``_LANGUAGES`` with that verified list.
Until then this table holds a small set of widely supported languages so the
prompt can name the target in English next to its code.

Codes are BCP 47 tags. Lookup is case insensitive, accepts ``_`` in place of
``-``, and falls back from a full tag (``pt-BR``) to its primary subtag
(``pt``) when the full tag has no entry of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Language:
    code: str          # canonical BCP 47 tag, for example "tr" or "pt-BR"
    english_name: str  # name used inside the prompt, for example "Turkish"


# Replace with W12's verified list.
_LANGUAGES: tuple[Language, ...] = (
    Language("ar", "Arabic"),
    Language("de", "German"),
    Language("en", "English"),
    Language("es", "Spanish"),
    Language("fr", "French"),
    Language("it", "Italian"),
    Language("ja", "Japanese"),
    Language("ko", "Korean"),
    Language("pt", "Portuguese"),
    Language("pt-BR", "Brazilian Portuguese"),
    Language("ru", "Russian"),
    Language("tr", "Turkish"),
    Language("zh", "Chinese (Simplified)"),
)

_BY_KEY = {lang.code.lower(): lang for lang in _LANGUAGES}


def _key(code: str) -> str:
    return code.strip().replace("_", "-").lower()


def primary_subtag(code: str) -> str:
    """``"pt-BR"`` gives ``"pt"``. Used to compare source and target languages."""
    return _key(code).split("-", 1)[0]


def find_language(code: Optional[str]) -> Optional[Language]:
    """The table entry for ``code``, or None when the code is empty or unknown."""
    if not code or not code.strip():
        return None
    key = _key(code)
    return _BY_KEY.get(key) or _BY_KEY.get(primary_subtag(key))


def resolve_target_language(code: str) -> Language:
    """The entry for a target language. Raises ValueError for a code the table does not list."""
    lang = find_language(code)
    if lang is None:
        known = ", ".join(sorted(l.code for l in _LANGUAGES))
        raise ValueError(f"Unsupported target language {code!r}. Supported codes: {known}.")
    return lang


def supported_languages() -> tuple[Language, ...]:
    return _LANGUAGES
