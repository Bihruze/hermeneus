"""``hermes hermeneus doctor``: what this Mac needs, in plain words, with a fix for each gap.

The doctor never opens an audio stream, never prompts for a permission and
never prints a secret.
"""

from __future__ import annotations

import platform
import re
import shutil
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import components
from .contracts import SecretGetter

SECRET_NAME = components.SECRET_NAME
MIN_MACOS = (13, 0)          # the virtual-device fallback
PROCESS_TAP_MACOS = (14, 2)  # AudioHardwareCreateProcessTap, the primary path


@dataclass
class Check:
    name: str
    ok: Optional[bool]          # None means "could not be read"
    detail: str
    fix: str = ""
    required: bool = True

    @property
    def id(self) -> str:
        return re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-")

    @property
    def state(self) -> str:
        """ok, warn (optional check failed, or could not be read) or fail (required check failed)."""
        if self.ok is True:
            return "ok"
        if self.ok is False and self.required:
            return "fail"
        return "warn"

    def to_dict(self) -> dict:
        out = {"id": self.id, "label": self.name, "state": self.state, "detail": self.detail}
        if self.fix and self.ok is not True:
            out["fix"] = self.fix
        return out


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok is not False for c in self.checks if c.required)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": [c.to_dict() for c in self.checks]}

    def render(self) -> str:
        marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
        lines = []
        for c in self.checks:
            lines.append(f"[{marks[c.state]}] {c.name}: {c.detail}")
            if c.fix and c.ok is not True:
                lines.append(f"       fix: {c.fix}")
        lines.append("All required checks passed." if self.ok else "Some required checks failed.")
        return "\n".join(lines)


def _macos(mac_ver: Callable[[], tuple]) -> Check:
    if sys.platform != "darwin":
        return Check("macOS", False, f"this is {sys.platform}; Hermeneus captures audio on macOS only",
                     "run Hermeneus on a Mac")
    release = mac_ver()[0] or ""
    try:
        version = tuple(int(p) for p in release.split(".")[:2])
    except ValueError:
        version = ()
    if version and version < MIN_MACOS:
        return Check("macOS", False, f"macOS {release} is older than {MIN_MACOS[0]}",
                     f"update to macOS {MIN_MACOS[0]} or newer")
    if version and version < PROCESS_TAP_MACOS:
        return Check("macOS", False,
                     f"macOS {release}: capture without a driver needs macOS 14.2; only the virtual device "
                     "fallback (BlackHole) works here",
                     "update to macOS 14.2 or newer, or install BlackHole for the fallback", required=False)
    return Check("macOS", True, f"macOS {release or 'unknown version'}")


def _tap_permission(verify: Callable[[], tuple[bool, str]]) -> Check:
    """System Audio Recording, proven with real audio through a short tap."""
    name = "permission: system audio recording"
    fix = ("allow the app that runs Hermes (your terminal app for hermes in a terminal, Hermes for the desktop "
           "app) under System Settings > Privacy & Security > Screen & System Audio Recording "
           "(System Audio Recording Only is enough), then run the doctor again")
    try:
        ok, detail = verify()
    except components.ComponentUnavailable:
        return Check(name, None, "the real-audio check is not part of this build; " + _UNREAD, fix, required=False)
    except Exception as exc:
        return Check(name, False, f"the test capture failed: {exc}", fix)
    return Check(name, ok, detail or ("audio arrived through the tap" if ok else "the tap delivered only silence"),
                 fix)


_UNREAD = ("macOS keeps this permission in a database that apps cannot read without asking for it or "
           "without Full Disk Access, so the doctor does not read it.")


def _microphone_permission() -> Check:
    """The virtual device is an input device, so macOS asks for microphone access."""
    return Check("permission: microphone (virtual device)", None,
                 _UNREAD + " The first start asks macOS; a refusal shows up as a capture error.",
                 "allow the app that runs Hermes under System Settings > Privacy & Security > Microphone, "
                 "then start again", required=False)


_AUDIO_CHECK_SKIPPED = ("skipped with --no-audio-check. Without it the doctor plays a short quiet test tone "
                        "(on BlackHole or VB-Cable when installed, so you do not hear it) and checks that it arrives.")


def _capture(verify: Optional[Callable[[], tuple[bool, str]]]) -> list[Check]:
    checks = []
    backends = components.capture_backends()
    if not backends:
        return [Check("capture backend", False, "this build has no capture backend",
                      "reinstall the plugin; capture/virtual_device.py is missing")]
    any_ok = False
    for backend in backends:
        try:
            ok, reason = backend.available()
        except Exception as exc:
            ok, reason = False, f"could not check: {exc}"
        any_ok = any_ok or ok
        if ok:
            fix = ""
        elif backend.name == "process-tap":
            fix = "process taps need macOS 14.2 or later; on older macOS use a virtual device such as BlackHole"
        else:
            fix = ("install BlackHole 2ch with: brew install blackhole-2ch (official source "
                   "https://github.com/ExistentialAudio/BlackHole), then run the doctor again")
        checks.append(Check(f"capture: {backend.name}", ok, reason, fix, required=False))
    tap = next((c for c in checks if c.name == "capture: process-tap"), None)
    if tap is not None and tap.ok:
        if verify is None:
            checks.append(Check("permission: system audio recording", None, _AUDIO_CHECK_SKIPPED, required=False))
        else:
            checks.append(_tap_permission(verify))
    checks.append(Check("audio source", any_ok,
                        "at least one capture backend can run" if any_ok else "no capture backend can run",
                        "" if any_ok else "see the capture lines above"))
    return checks


def _multi_output(process_tap_ok: bool) -> Check:
    if process_tap_ok:
        return Check("system sound reaches the virtual device", True,
                     "not needed: the process tap reads system audio directly", required=False)
    try:
        report = components.detect_multi_output_setup()
    except components.ComponentUnavailable as exc:
        return Check("system sound reaches the virtual device", None, str(exc), required=False)
    except Exception as exc:
        return Check("system sound reaches the virtual device", None, f"could not read the output setup: {exc}",
                     required=False)
    ok = bool(getattr(report, "ok", False))
    detail = getattr(report, "explanation", "") or f"default output: {getattr(report, 'default_output', None)}"
    fix = "" if ok else ("open Audio MIDI Setup, create a Multi-Output Device with your speakers or headphones "
                         "and BlackHole 2ch, and choose it as the system output in System Settings > Sound")
    return Check("system sound reaches the virtual device", ok, detail, fix)


def _secret(get_secret: SecretGetter) -> Check:
    try:
        value = get_secret(SECRET_NAME)
    except Exception as exc:
        return Check(SECRET_NAME, False, f"could not be read in this profile: {type(exc).__name__}",
                     f"run hermes plugins install again or add {SECRET_NAME} with hermes config set")
    if value:
        return Check(SECRET_NAME, True, "set (value not shown)")
    return Check(SECRET_NAME, False, "not set",
                 f"create a key at https://elevenlabs.io/app/settings/api-keys and add it to Hermes as {SECRET_NAME}")


def _aux(aux_config: Optional[dict]) -> Check:
    cfg = aux_config or {}
    model = str(cfg.get("model") or "").strip()
    provider = str(cfg.get("provider") or "auto").strip() or "auto"
    if model:
        return Check("translation model", True, f"auxiliary.live_translate uses {provider}/{model}", required=False)
    return Check("translation model", False,
                 "auxiliary.live_translate has no model, so translation uses your main model; "
                 "a large main model adds seconds per line",
                 "pick a fast model with: hermes model (Configure auxiliary models > Live translation)",
                 required=False)


def _ffmpeg(which: Callable[[str], Optional[str]]) -> Check:
    found = which("ffmpeg")
    return Check("ffmpeg", True, ("found, but not needed" if found else "not installed; not needed"),
                 required=False)


def run(*, get_secret: SecretGetter, aux_config: Optional[dict],
        mac_ver: Callable[[], tuple] = platform.mac_ver,
        which: Callable[[str], Optional[str]] = shutil.which,
        verify_tap_audio: Callable[[], tuple[bool, str]] = components.verify_tap_audio,
        audio_check: bool = True) -> DoctorReport:
    """Process tap first. The virtual-device and Multi-Output lines describe the fallback and
    never fail the report while the process tap works. Windows has its own checks (doctor_windows)."""
    if sys.platform == "win32":
        from .doctor_windows import run as run_windows
        return run_windows(get_secret=get_secret, aux_config=aux_config, which=which)
    report = DoctorReport()
    report.checks.append(_macos(mac_ver))
    capture = _capture(verify_tap_audio if audio_check else None)
    report.checks.extend(capture)
    tap_ok = any(c.name == "capture: process-tap" and c.ok for c in capture)
    perm = next((c for c in capture if c.name == "permission: system audio recording"), None)
    tap_works = tap_ok and (perm is None or perm.ok is not False)
    if not tap_ok:
        report.checks.append(_microphone_permission())
    mo = _multi_output(tap_works)
    if tap_works:
        mo.required = False
    report.checks.append(mo)
    report.checks.append(_secret(get_secret))
    report.checks.append(_aux(aux_config))
    report.checks.append(_ffmpeg(which))
    return report
