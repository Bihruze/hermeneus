"""Which ElevenLabs voice speaks the translation.

Order: the per-session ``--voice`` flag, then the ``voice_id`` plugin
setting, then ``DEFAULT_VOICE_ID``. This module has no third-party imports so
option parsing can use it without loading numpy or websockets.

Default voice, checked 2026-11-11:

* The help center page "What are Default voices?"
  (https://elevenlabs.io/docs/help-center/product/voices/my-voices/what-are-default-voices)
  says "All our Default voices will expire on December 31, 2026" and that
  they "are only available for accounts that were created before March
  2026". No legacy Default voice stays, so none of them can be the default.
* The same page lists replacement voices "that you will be able to use
  forever". Talia (``OZ0L6eISlOejga3XjDFt``) is the listed replacement for
  Sarah (``EXAVITQu4vr4xnSDxMaL``).
* ``GET /v1/shared-voices?search=Talia`` on 2026-11-11 returned
  ``free_users_allowed: true``, ``rate: 1.0``, ``notice_period: 4015`` (days),
  use case ``informative_educational``, and ``verified_languages`` entries for
  ``eleven_flash_v2_5`` in ar, cs, de, en, es, hr, hu, nl, no, pl, pt, ta, tr, uk.
"""

from __future__ import annotations

from typing import Optional

DEFAULT_VOICE_ID = "OZ0L6eISlOejga3XjDFt"
DEFAULT_VOICE_NAME = "Talia"


def resolve_voice(session_voice: Optional[str], configured_voice: Optional[str]) -> str:
    """The voice id for a session: flag, then setting, then the default."""
    for value in (session_voice, configured_voice):
        text = (value or "").strip()
        if text:
            return text
    return DEFAULT_VOICE_ID


def unavailable_message(voice_id: str) -> str:
    return (f"Voice {voice_id} is not available on this ElevenLabs account; "
            f"using the default voice {DEFAULT_VOICE_NAME}.")
