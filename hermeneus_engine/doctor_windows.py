"""``hermes hermeneus doctor`` on Windows: the same report shape and rules as on macOS.

Like the macOS doctor it never opens an audio stream, never prompts and never
prints a secret. The key and translation model checks are the macOS ones.
"""

from __future__ import annotations

import shutil
import sys
from typing import Any, Callable, Optional

from . import components
from .contracts import SecretGetter
from .doctor import Check, DoctorReport, _aux, _ffmpeg, _secret

PROCESS_LOOPBACK_BUILD = 20348  # AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS minimum client build
VB_CABLE_URL = "https://vb-audio.com/Cable/"


def _windows_build() -> Optional[int]:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        return None


def _windows(build: Optional[int]) -> Check:
    if build is None:
        return Check("Windows", None, "the Windows build number could not be read", required=False)
    if build < PROCESS_LOOPBACK_BUILD:
        return Check("Windows", False,
                     f"build {build}: capture without a driver needs build {PROCESS_LOOPBACK_BUILD} or later "
                     "(Windows 11); only the virtual device fallback (VB-CABLE) works here",
                     f"update Windows, or install VB-CABLE ({VB_CABLE_URL}) for the fallback", required=False)
    return Check("Windows", True, f"build {build}")


def _capture() -> list[Check]:
    checks = []
    backends = components.capture_backends()
    any_ok = False
    for backend in backends:
        try:
            ok, reason = backend.available()
        except Exception as exc:
            ok, reason = False, f"could not check: {exc}"
        any_ok = any_ok or ok
        if ok:
            fix = ""
        elif backend.name == "process-loopback":
            fix = (f"process loopback needs Windows build {PROCESS_LOOPBACK_BUILD} or later; on older Windows "
                   "use a virtual device such as VB-CABLE")
        else:
            fix = (f"install VB-CABLE from {VB_CABLE_URL}, set CABLE Input as the default output or route the "
                   "app to it, then run the doctor again")
        checks.append(Check(f"capture: {backend.name}", ok, reason, fix, required=False))
    checks.append(Check("audio source", any_ok,
                        "at least one capture backend can run" if any_ok else "no capture backend can run",
                        "" if any_ok else "see the capture lines above"))
    return checks


def _output_device(sd_loader: Callable[[], Any]) -> Check:
    """A default output exists (spoken translation plays there unless --device says otherwise)."""
    name = "audio output device"
    try:
        sd = sd_loader()
        info = sd.query_devices(kind="output")
    except Exception as exc:
        return Check(name, False, f"no default output device could be read: {exc}",
                     "connect speakers or headphones and pick them under Settings > System > Sound",
                     required=False)
    return Check(name, True, f"default output: {info.get('name', 'unknown')}", required=False)


def _microphone_privacy() -> Check:
    """Counterpart of the macOS permission lines. Windows documents no permission for process loopback;
    the microphone privacy switch is the one setting that could plausibly silence it, and it does gate a
    VB-CABLE input. The doctor does not read it (the consent store is not a documented interface)."""
    return Check("permission: microphone (Windows privacy)", None,
                 "the doctor does not read this setting. If capture delivers only silence, it is the first "
                 "thing to check",
                 "open Settings > Privacy & security > Microphone and turn on Let desktop apps access your "
                 "microphone, then start again", required=False)


def _vb_cable_route(loopback_ok: bool, sd_loader: Callable[[], Any]) -> Check:
    """Counterpart of the macOS Multi-Output check for the VB-CABLE fallback."""
    name = "system sound reaches the virtual device"
    if loopback_ok:
        return Check(name, True, "not needed: process loopback reads system audio directly", required=False)
    try:
        out = str(sd_loader().query_devices(kind="output").get("name", ""))
    except Exception as exc:
        return Check(name, None, f"could not read the default output: {exc}", required=False)
    if "cable input" in out.casefold():
        return Check(name, True, f"the default output is '{out}', so CABLE Output receives system sound. To hear "
                     "it too, turn on Listen to this device for CABLE Output (Sound control panel > Recording "
                     "> CABLE Output > Properties > Listen)", required=False)
    return Check(name, False, f"sound output goes to '{out}' only, so CABLE Output receives silence",
                 "choose CABLE Input as the output under Settings > System > Sound (or as the app's output), "
                 "then turn on Listen to this device for CABLE Output to keep hearing it")


def _load_sounddevice():
    import sounddevice
    return sounddevice


def run(*, get_secret: SecretGetter, aux_config: Optional[dict],
        build: Optional[Callable[[], Optional[int]]] = None,
        which: Callable[[str], Optional[str]] = shutil.which,
        sd_loader: Optional[Callable[[], Any]] = None) -> DoctorReport:
    report = DoctorReport()
    report.checks.append(_windows((build or _windows_build)()))
    capture = _capture()
    report.checks.extend(capture)
    loopback_ok = any(c.name == "capture: process-loopback" and c.ok for c in capture)
    report.checks.append(_microphone_privacy())
    report.checks.append(_vb_cable_route(loopback_ok, sd_loader or _load_sounddevice))
    report.checks.append(_output_device(sd_loader or _load_sounddevice))
    report.checks.append(_secret(get_secret))
    report.checks.append(_aux(aux_config))
    report.checks.append(_ffmpeg(which))
    return report
