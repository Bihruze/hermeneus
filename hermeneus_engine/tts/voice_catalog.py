"""The voices on the user's ElevenLabs account, for ``hermes hermeneus voices`` and GET /voices.

Source: ``GET https://api.elevenlabs.io/v2/voices``
(https://elevenlabs.io/docs/api-reference/voices/search). ``page_size`` is at
most 100; ``has_more`` and ``next_page_token`` page through the rest. Each
voice has ``voice_id``, ``name``, ``category`` and ``verified_languages``
(``language``, ``model_id``, ``accent``, ``locale``). The list keeps the
languages verified for ``eleven_flash_v2_5``, the model that speaks the
translation.

The key is read through the caller's secret getter on every fetch, sent only
in the ``xi-api-key`` header and never logged. Answers are cached for
``CACHE_TTL_S`` per key so the pane's picker does not call ElevenLabs on
every render. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from .voices import DEFAULT_VOICE_ID

VOICES_URL = "https://api.elevenlabs.io/v2/voices"
FLASH_MODEL_ID = "eleven_flash_v2_5"
PAGE_SIZE = 100
MAX_PAGES = 10
TIMEOUT_S = 8.0
CACHE_TTL_S = 60.0
KEY_NAME = "ELEVENLABS_API_KEY"


class VoiceListError(RuntimeError):
    """The voice list could not be read. The message is plain language and never contains the key."""


@dataclass(frozen=True)
class VoiceInfo:
    voice_id: str
    name: str
    category: str = ""
    flash_languages: tuple[str, ...] = ()
    is_default: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["flash_languages"] = list(self.flash_languages)
        return d


@dataclass
class VoiceList:
    voices: list[VoiceInfo] = field(default_factory=list)
    fetched_at: float = 0.0

    def to_dict(self) -> dict:
        return {"voices": [v.to_dict() for v in self.voices], "default_voice_id": DEFAULT_VOICE_ID,
                "fetched_at": self.fetched_at}


# fetch(url, headers, timeout) -> parsed JSON body; raises urllib errors.
Fetch = Callable[[str, dict, float], dict]


def _http_fetch(url: str, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed https URL)
        return json.loads(resp.read().decode("utf-8"))


def parse_voice(raw: dict) -> Optional[VoiceInfo]:
    voice_id = str(raw.get("voice_id") or "").strip()
    if not voice_id:
        return None
    langs: list[str] = []
    for entry in raw.get("verified_languages") or []:
        if not isinstance(entry, dict) or entry.get("model_id") != FLASH_MODEL_ID:
            continue
        code = str(entry.get("language") or "").strip()
        if code and code not in langs:
            langs.append(code)
    return VoiceInfo(voice_id=voice_id, name=str(raw.get("name") or voice_id), category=str(raw.get("category") or ""),
                     flash_languages=tuple(langs), is_default=voice_id == DEFAULT_VOICE_ID)


def describe_error(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return (f"Voices: ElevenLabs rejected the API key (HTTP 401). Check {KEY_NAME} in Hermes "
                    "(see: hermes hermeneus doctor).")
        if exc.code == 403:
            return ("Voices: the ElevenLabs API key may not list voices (HTTP 403). Allow Voices access for the key "
                    "at https://elevenlabs.io/app/settings/api-keys.")
        if exc.code == 429:
            return "Voices: ElevenLabs is rate limiting this account (HTTP 429). Try again in a minute."
        return f"Voices: ElevenLabs answered HTTP {exc.code}. Try again in a minute."
    if isinstance(exc, (urllib.error.URLError, TimeoutError, OSError)):
        reason = getattr(exc, "reason", exc)
        return f"Voices: could not reach ElevenLabs ({reason}). Check the network connection and try again."
    if isinstance(exc, ValueError):
        return "Voices: ElevenLabs sent an answer this plugin cannot read. Try again in a minute."
    return f"Voices: the list could not be read ({type(exc).__name__}). Try again in a minute."


class VoiceCatalog:
    def __init__(self, get_key: Callable[[], Optional[str]], *, fetch: Fetch = _http_fetch,
                 ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._get_key = get_key
        self._fetch = fetch
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, VoiceList]] = {}

    def list(self, *, refresh: bool = False) -> VoiceList:
        """Every voice on the account, the default voice first, then by name. Raises VoiceListError. Blocking."""
        try:
            key = self._get_key()
        except Exception as exc:
            raise VoiceListError(f"Voices: {KEY_NAME} could not be read in this profile ({type(exc).__name__}). "
                                 "Run: hermes hermeneus doctor") from None
        if not key:
            raise VoiceListError(f"Voices: no ElevenLabs API key is set. Add {KEY_NAME} to Hermes "
                                 "(see: hermes hermeneus doctor).")
        slot = hashlib.sha256(key.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._lock:
            hit = self._cache.get(slot)
            if hit is not None and not refresh and now - hit[0] < self._ttl:
                return hit[1]
        voices = self._fetch_all(key)
        result = VoiceList(voices=voices, fetched_at=time.time())
        with self._lock:
            self._cache = {slot: (now, result)}
        return result

    def cached(self) -> Optional[VoiceList]:
        """The last list fetched within the cache time, without asking ElevenLabs; None when there is none."""
        now = self._clock()
        with self._lock:
            for fetched_at, result in self._cache.values():
                if now - fetched_at < self._ttl:
                    return result
        return None

    def _fetch_all(self, key: str) -> list[VoiceInfo]:
        seen: dict[str, VoiceInfo] = {}
        token: Optional[str] = None
        for _ in range(MAX_PAGES):
            query = {"page_size": PAGE_SIZE, "include_total_count": "false"}
            if token:
                query["next_page_token"] = token
            url = f"{VOICES_URL}?{urllib.parse.urlencode(query)}"
            try:
                body = self._fetch(url, {"xi-api-key": key, "accept": "application/json"}, TIMEOUT_S)
            except Exception as exc:
                raise VoiceListError(describe_error(exc)) from None
            if not isinstance(body, dict) or not isinstance(body.get("voices"), list):
                raise VoiceListError(describe_error(ValueError("shape")))
            for raw in body["voices"]:
                info = parse_voice(raw) if isinstance(raw, dict) else None
                if info is not None:
                    seen.setdefault(info.voice_id, info)
            token = body.get("next_page_token")
            if not body.get("has_more") or not token:
                break
        return sorted(seen.values(), key=lambda v: (not v.is_default, v.name.casefold()))
