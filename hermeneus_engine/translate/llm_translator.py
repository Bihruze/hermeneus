"""Segment translation through the injected Hermes LLM call.

``LlmTranslator`` implements the ``Translator`` protocol from
``hermeneus_engine.contracts``. It also accepts speaker labels and earlier
translations through :class:`~hermeneus_engine.translate.prompt.ContextLine`,
which :class:`~hermeneus_engine.translate.scheduler.TranslationScheduler`
uses.

The LLM call is the ``LlmCall`` callable the plugin layer injects. The
translator owns the deadline: the Hermes host may retry once and walk its
fallback chain before it raises, so a per-request timeout on the host side
does not bound the total time of one call. ``asyncio.wait_for`` here does.
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional, Sequence, Union

from ..contracts import LlmCall, TranslationError
from .languages import primary_subtag, resolve_target_language
from .prompt import ContextLine, build_messages, clean_reply, guard_reply

# A healthy one-line translation is a short prompt and a short reply. Six
# seconds leaves room for a slow first token while still failing well before
# the line is stale. The Hermes aux default of 30 s
# (agent/auxiliary_client.py:6082) would hold a subtitle for half a minute.
DEFAULT_TIMEOUT_S = 6.0

# Three earlier lines cover the usual reach of a pronoun or a repeated name in
# conversation. With translations included each line costs roughly 40 to 80
# tokens, so the context stays under about 250 tokens of prefill.
DEFAULT_CONTEXT_SEGMENTS = 3

# Bounds one context line (original or translation). A long monologue line
# would otherwise grow every later prompt. The tail is kept.
DEFAULT_CONTEXT_CHARS_PER_LINE = 300

_MAX_ERROR_DETAIL = 200
_HAS_WORD = re.compile(r"\w")

ContextItem = Union[str, ContextLine]


def _plain(exc: BaseException) -> str:
    detail = " ".join(str(exc).split())
    if len(detail) > _MAX_ERROR_DETAIL:
        detail = detail[:_MAX_ERROR_DETAIL - 3] + "..."
    name = type(exc).__name__
    return f"{name}: {detail}" if detail else name


class LlmTranslator:
    """Translate one committed segment per call with the injected ``LlmCall``."""

    def __init__(self, llm: LlmCall, *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 context_segments: int = DEFAULT_CONTEXT_SEGMENTS,
                 context_chars_per_line: int = DEFAULT_CONTEXT_CHARS_PER_LINE,
                 skip_same_language: bool = True) -> None:
        """``skip_same_language``: when the detected source language has the same primary
        subtag as the target, return the text without a model call. The prompt asks the model
        to return such text unchanged anyway, so this only saves a round trip. It leaves
        foreign words inside a mostly same-language segment untranslated."""
        if timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        if context_segments < 0:
            raise ValueError("context_segments must be zero or more")
        self._llm = llm
        self.timeout_s = float(timeout_s)
        self.context_segments = int(context_segments)
        self.context_chars_per_line = int(context_chars_per_line)
        self.skip_same_language = skip_same_language

    async def translate(self, text: str, *, source_language: Optional[str], target_language: str,
                        context: Sequence[ContextItem], speaker_id: Optional[str] = None,
                        timeout_s: Optional[float] = None) -> str:
        """Translate ``text`` into ``target_language``.

        ``context`` holds earlier segments, oldest first, as plain strings or ``ContextLine``.
        Only the last ``context_segments`` entries are sent. ``timeout_s`` overrides the
        instance timeout for this call. Raises ``TranslationError`` on timeout, on any failure
        of the LLM call, on an empty reply, and on a reply that is not only a translation
        (``prompt.reply_problem``). Cancellation propagates unchanged.
        """
        target = resolve_target_language(target_language)
        stripped = text.strip()
        if not stripped:
            raise TranslationError("Nothing to translate: the segment is empty.")
        if not _HAS_WORD.search(stripped):
            return stripped  # punctuation only, such as "..." or "?"
        if (self.skip_same_language and source_language
                and primary_subtag(source_language) == primary_subtag(target.code)):
            return stripped

        lines = [item if isinstance(item, ContextLine) else ContextLine(text=item) for item in context]
        lines = [line for line in lines if line.text.strip()]
        lines = lines[-self.context_segments:] if self.context_segments else []
        messages = build_messages(stripped, target=target, speaker_id=speaker_id,
                                  source_language=source_language, context=lines,
                                  context_chars_per_line=self.context_chars_per_line)

        budget = self.timeout_s if timeout_s is None else float(timeout_s)
        if budget <= 0:
            raise TranslationError("Translation skipped: no time left for this line.")
        try:
            raw = await asyncio.wait_for(self._llm(messages), timeout=budget)
        except asyncio.CancelledError:
            raise
        except TimeoutError as exc:
            raise TranslationError(f"Translation timed out after {budget:g} s.") from exc
        except TranslationError:
            raise  # the LlmCall already explained the failure in plain words
        except Exception as exc:
            raise TranslationError(f"Translation failed: {_plain(exc)}") from exc

        if not isinstance(raw, str):
            raise TranslationError(f"Translation failed: the model call returned {type(raw).__name__}, not text.")
        cleaned = clean_reply(raw, original=stripped, target=target)
        if not cleaned:
            raise TranslationError("Translation failed: the model returned an empty reply.")
        # Only the translation goes on screen: the corrected part of a reply that marks one, or the
        # reply without a short copied fragment at its edge; a reply with notes, copied speech or the
        # wrong script fails the line ("Not translated"). String checks, no second model call.
        guarded = guard_reply(cleaned, original=stripped, target=target, source_language=source_language)
        if guarded.problem:
            raise TranslationError(f"Translation rejected: {guarded.problem}.")
        return guarded.text
