"""Keeps API keys out of error texts and logs.

Speech to text and spoken translation both read the ElevenLabs key again on
every reconnect, so a rotated key and its predecessor can both appear in one
error text. ``KeyRedactor`` remembers the last few keys it was given and
replaces each of them.
"""

from __future__ import annotations

REDACTED = "[redacted]"
KEEP_KEYS = 4


class KeyRedactor:
    def __init__(self, keep: int = KEEP_KEYS) -> None:
        self._keep = keep
        self._keys: list[str] = []

    def remember(self, key: str) -> None:
        if not key or key in self._keys:
            return
        self._keys.append(key)
        del self._keys[:-self._keep]

    def redact(self, text: str) -> str:
        for key in self._keys:
            text = text.replace(key, REDACTED)
        return text
