"""Target languages the translator can name in its prompt.

The data comes from ``hermeneus_engine.languages``, the verified table of the
languages ElevenLabs documents for Scribe v3. Every listening language is also
a subtitle target. Translation quality depends on the user's model.

Lookup accepts ISO 639-1, ISO 639-3 and BCP 47 tags (``tr``, ``tur``,
``tr-TR``, ``pt-BR``), ignores case and accepts ``_`` in place of ``-``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from hermeneus_engine import languages as verified


@dataclass(frozen=True)
class Language:
    code: str          # ISO 639-1 when one exists, otherwise ISO 639-3
    english_name: str  # name used inside the prompt, for example "Turkish"


def _from_verified(lang: verified.Language) -> Language:
    return Language(lang.iso639_1 or lang.iso639_3, lang.english_name)


_LANGUAGES: tuple[Language, ...] = tuple(
    _from_verified(verified.get(code)) for code in verified.target_codes()
)


def primary_subtag(code: str) -> str:
    """A comparable key for a language tag: the ISO 639-3 code when the table knows it.

    Scribe reports detected languages as ISO 639-3 (``eng``) while users pick
    targets as ISO 639-1 (``en``); both map to the same key here.
    """
    primary = code.strip().replace("_", "-").split("-", 1)[0].lower()
    try:
        return verified.normalize(primary)
    except verified.UnknownLanguageError:
        return primary


def find_language(code: Optional[str]) -> Optional[Language]:
    """The table entry for ``code``, or None when the code is empty or unknown."""
    if not code or not code.strip():
        return None
    try:
        return _from_verified(verified.get(code))
    except verified.UnknownLanguageError:
        return None


def resolve_target_language(code: str) -> Language:
    """The entry for a target language. Raises ValueError for a code the table does not list."""
    lang = find_language(code)
    if lang is None:
        raise ValueError(
            f"Unsupported target language {code!r}. Use an ISO 639-1 or 639-3 code from the "
            f"Scribe v3 language list, for example tr, en, de."
        )
    return lang


def supported_languages() -> tuple[Language, ...]:
    return _LANGUAGES
