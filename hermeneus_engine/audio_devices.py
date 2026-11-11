"""Device-name rules shared by capture, playback and routing.

* One endpoint, several entries. PortAudio on Windows lists each endpoint once
  per host API (MME, DirectSound, WASAPI, WDM-KS) under the same or a similar
  name (capture/virtual_device.py ``resolve_input_device`` notes this and keeps
  its own copy of the rule so the macOS capture path stays unchanged). When a
  name matches more than one entry, the WASAPI entry wins, then the default host
  API's entry. With one match nothing changes, which is every macOS case.
* One loopback cable, two names. VB-CABLE has a playback side "CABLE Input
  (VB-Audio Virtual Cable)" and a recording side "CABLE Output (VB-Audio Virtual
  Cable)". Audio played to the first comes out of the second, so for feedback
  they are one device (BlackHole uses one name for both sides on macOS).
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

WASAPI_HOSTAPI_NAME = "Windows WASAPI"  # the PortAudio host API name sounddevice reports


def prefer_one(matches: Sequence[int], devices: Sequence[dict], hostapis: Callable[[], Sequence[dict]],
               default_hostapi: Callable[[], Optional[int]]) -> list[int]:
    """Narrow several device indices for one name to the WASAPI entry, else the default host API's."""
    if len(matches) <= 1:
        return list(matches)
    try:
        apis = list(hostapis())
    except Exception:
        apis = []
    wasapi = {i for i, a in enumerate(apis) if str(a.get("name", "")) == WASAPI_HOSTAPI_NAME}
    picked = [m for m in matches if devices[m].get("hostapi") in wasapi]
    if len(picked) == 1:
        return picked
    try:
        default = default_hostapi()
    except Exception:
        default = None
    if default is not None:
        on_default = [m for m in matches if devices[m].get("hostapi") == default]
        if len(on_default) == 1:
            return on_default
    return list(matches)


_VB_SIDE = re.compile(r"\b(input|output)\b")


def loopback_pair_key(name: str) -> Optional[str]:
    """A key shared by both sides of a VB-Audio cable, or None for any other device."""
    folded = (name or "").casefold()
    if "vb-audio" not in folded:
        return None
    return re.sub(r"\s+", " ", _VB_SIDE.sub("", folded)).strip()


def same_loopback_device(a: Optional[str], b: Optional[str]) -> bool:
    ka, kb = loopback_pair_key(a or ""), loopback_pair_key(b or "")
    return ka is not None and ka == kb
