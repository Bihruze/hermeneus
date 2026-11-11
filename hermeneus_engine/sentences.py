"""Early translation at stable sentence boundaries.

Scribe commits a segment only when the speaker pauses. On continuous speech
such as a news read a segment can hold 30 to 40 s of speech, so translating
only on commit puts the translation of the first sentence half a minute
behind its audio. ``SentenceSplitter`` watches the growing partial
transcript of the current segment and releases a sentence for translation
as soon as it is complete and has stopped changing:

* complete: terminal punctuation followed by more words, so Scribe has moved
  on past the sentence end;
* stable: the sentence text stayed the same for ``stable_ms`` and across at
  least ``stable_partials`` partials.

What has been released is remembered as a word sequence. Later partials and
the final commit are aligned against it word by word, so text that was
released is never translated again, and text after it is never lost even
when Scribe revises a word inside the released part.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

# Chosen by replaying six recorded Scribe v3 Live partial streams (110
# sentences of continuous news speech, W03's raw probe runs) through this
# class with T in {0, 150, 300, 500, 800} ms and N in {1, 2, 3}: no released
# sentence differed from the final transcript at any setting, and release
# delay grew with T (p50 0.64 s at T=0, 1.11 s at T=300). Scribe recomputes
# only the last few tokens of a partial (max_tokens_to_recompute 5 in the
# session config), so a sentence that more words follow is already settled.
# N=2 costs nothing measurable here and guards against a single odd partial.
DEFAULT_STABLE_MS = 0
DEFAULT_STABLE_PARTIALS = 2
# A one or two word "sentence" ("Yes." "Thank you.") is held and joined to
# the next one; very short lines translate badly and flicker.
DEFAULT_MIN_WORDS = 3

_TERMINAL = ".!?…。！？؟।"
_CLOSERS = "\"'”’»)]}"
# Terminal punctuation, optional closing quotes or brackets, whitespace, then
# the start of the next word.
_BOUNDARY = re.compile(rf"[{re.escape(_TERMINAL)}]+[{re.escape(_CLOSERS)}]*(?=\s+\S)")
# A period after these tokens does not end a sentence.
_ABBREVIATIONS = frozenset({
    "mr", "mrs", "ms", "dr", "prof", "st", "sr", "jr", "vs", "etc", "e.g", "i.e", "inc", "ltd",
    "co", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})


def _norm(word: str) -> str:
    stripped = "".join(ch for ch in unicodedata.normalize("NFKC", word).casefold()
                       if unicodedata.category(ch)[0] in "LN")
    return stripped


def _words(text: str) -> list[str]:
    return text.split()


def split_sentences(text: str, *, min_words: int = DEFAULT_MIN_WORDS) -> tuple[list[str], str]:
    """Complete sentences at the front of ``text`` and the unfinished rest.

    A sentence counts as complete only when more words follow it. Sentences
    shorter than ``min_words`` are joined to the next one.
    """
    sentences: list[str] = []
    start = 0
    pending = ""
    for match in _BOUNDARY.finditer(text):
        end = match.end()
        piece = text[start:end].strip()
        last = _words(piece)[-1] if piece else ""
        token = last.rstrip(_TERMINAL + _CLOSERS).casefold()
        if token in _ABBREVIATIONS or (len(token) == 1 and token.isalpha()):
            continue  # "Dr. Smith", "J. Smith"
        candidate = f"{pending} {piece}".strip() if pending else piece
        if len(_words(candidate)) < min_words:
            pending = candidate
        else:
            sentences.append(candidate)
            pending = ""
        start = end
    rest = text[start:].strip()
    if pending:
        rest = f"{pending} {rest}".strip()
    return sentences, rest


def split_all(text: str, *, min_words: int = DEFAULT_MIN_WORDS) -> list[str]:
    """Every sentence of a final text, the unfinished tail included."""
    sentences, rest = split_sentences(text, min_words=min_words)
    if rest:
        if sentences and len(_words(rest)) < min_words and rest[-1:] not in _TERMINAL:
            sentences[-1] = f"{sentences[-1]} {rest}"
        else:
            sentences.append(rest)
    return sentences


def after_released(released: list[str], text: str) -> str:
    """The part of ``text`` that comes after the released words.

    ``released`` holds the normalized words already sent for translation.
    The common case is an exact prefix. When Scribe revised a word inside
    the released part, the words are aligned and the cut goes after the
    last released word that matches.
    """
    words = _words(text)
    if not released:
        return text.strip()
    normed = [_norm(w) for w in words]
    n = len(released)
    if normed[:n] == released:
        return " ".join(words[n:])
    matcher = difflib.SequenceMatcher(None, released, normed, autojunk=False)
    blocks = [b for b in matcher.get_matching_blocks() if b.size]
    if not blocks:
        # Nothing in common: the partial restarted. Keep the released words
        # out by length so nothing is translated twice.
        return " ".join(words[n:])
    last = blocks[-1]
    unmatched_tail = n - (last.a + last.size)
    cut = min(len(words), last.b + last.size + max(unmatched_tail, 0))
    return " ".join(words[cut:])


@dataclass
class _Candidate:
    text: str
    first_seen_ns: int
    partials: int = 1


@dataclass
class SentenceSplitter:
    stable_ms: float = DEFAULT_STABLE_MS
    stable_partials: int = DEFAULT_STABLE_PARTIALS
    min_words: int = DEFAULT_MIN_WORDS
    released: list[str] = field(default_factory=list)
    _candidate: Optional[_Candidate] = None

    @property
    def enabled(self) -> bool:
        return self.stable_ms >= 0

    def remainder(self, text: str) -> str:
        return after_released(self.released, text)

    def on_partial(self, text: str, now_ns: int) -> list[str]:
        """Sentences of this partial that are now stable, oldest first. Each is released once."""
        if not self.enabled:
            return []
        out: list[str] = []
        while True:
            sentences, _rest = split_sentences(self.remainder(text), min_words=self.min_words)
            if not sentences:
                self._candidate = None
                return out
            first = sentences[0]
            c = self._candidate
            if c is None or c.text != first:
                self._candidate = _Candidate(first, now_ns)
                return out
            if c is not None and c.partials == 0:
                c.partials = 1
            stable = (now_ns - c.first_seen_ns) >= self.stable_ms * 1e6 and c.partials >= self.stable_partials
            if not stable:
                c.partials += 1
                stable = (now_ns - c.first_seen_ns) >= self.stable_ms * 1e6 and c.partials >= self.stable_partials
                if not stable:
                    return out
            self.released.extend(_norm(w) for w in _words(first))
            out.append(first)
            # The next complete sentence, if there is one, starts its own
            # stability clock now.
            self._candidate = None

    def on_commit(self, text: str) -> list[str]:
        """Final sentences of the segment that were not released yet. Resets for the next segment."""
        rest = self.remainder(text)
        self.released = []
        self._candidate = None
        if not rest:
            return []
        if not self.enabled:
            return [rest]  # the old behaviour: one line per Scribe segment
        return split_all(rest, min_words=self.min_words)
