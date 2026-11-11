"""Import graph per platform: no macOS module loads on Windows, and no Windows module loads on macOS.

Each case runs a fresh interpreter, sets ``sys.platform`` before the first
Hermeneus import, exercises the platform-dependent paths (backend list,
availability, doctor, routing inventory, session lock probe) and reports
every module that got loaded. Modules that read ``sys.platform`` at import
time (asyncio, subprocess, and sounddevice, which picks its PortAudio library
by platform) are imported first with the real value, so the simulation only
reaches Hermeneus code.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PROBE = r"""
import asyncio, ctypes, json, shutil, signal, socket, subprocess, sys, tempfile
import numpy, sounddevice
baseline = set(sys.modules)
sys.path.insert(0, {root!r})
sys.platform = {platform!r}
import builtins
_real_import = builtins.__import__
asked = set()
def _recording_import(name, globals=None, locals=None, fromlist=(), level=0):
    importer = (globals or {{}}).get("__name__", "")
    if importer.startswith("hermeneus_engine") and level == 0:
        asked.add(name)
    return _real_import(name, globals, locals, fromlist, level)
builtins.__import__ = _recording_import
from hermeneus_engine import components, control, doctor, ipc, options, runtime, session
from hermeneus_engine.overlay import control as overlay_control
try:
    overlay_control.check_available()
except overlay_control.OverlayUnavailable:
    pass
from hermeneus_engine.tts import routing, playback
names = [b.name for b in components.capture_backends()]
avail = {{b.name: b.available()[0] for b in components.capture_backends()}}
routing.load_inventory()
d = tempfile.mkdtemp()
runtime.lock_holder(__import__("pathlib").Path(d))
if sys.platform == "darwin":
    doctor._macos(lambda: ("15.1", "", ""))
print(json.dumps({{"backends": names, "available": avail, "modules": sorted(set(sys.modules) - baseline), "asked": sorted(asked)}}))
"""

MACOS_ONLY = ("CoreAudio", "objc", "Foundation", "AppKit", "Cocoa",
              "hermeneus_engine.capture.process_tap", "hermeneus_engine.capture._coreaudio",
              "hermeneus_engine.capture._tap_engine", "hermeneus_engine.capture._pa_reader",
              "hermeneus_engine.capture.tap_doctor", "hermeneus_engine.tts.coreaudio_devices")
WINDOWS_ONLY = ("comtypes", "pycaw", "psutil", "msvcrt", "_winapi", "winreg",
                "hermeneus_engine.capture.process_loopback", "hermeneus_engine.capture._wasapi",
                "hermeneus_engine.capture._loopback_engine", "hermeneus_engine.capture._loopback_worker",
                "hermeneus_engine._windows", "hermeneus_engine.doctor_windows")


def probe(platform: str) -> dict:
    code = PROBE.format(root=str(ROOT), platform=platform)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def loaded(modules, forbidden):
    return sorted(m for m in modules for f in forbidden if m == f or m.startswith(f + "."))


# Both cases run on every host. On a Windows host the win32 case is the real platform and
# the darwin case is the simulation; on a Mac it is the other way round. Only modules loaded
# after the probe's baseline count, and the host's own standard library (fcntl on macOS,
# msvcrt and _winapi on Windows) is checked through what Hermeneus asks for instead.

def test_windows_loads_no_macos_module():
    result = probe("win32")
    assert result["backends"] == ["virtual-device", "process-loopback"]
    if sys.platform != "win32":
        assert result["available"]["process-loopback"] is False  # simulated: no Windows build number
    assert loaded(result["modules"], MACOS_ONLY) == []
    assert not {"fcntl", "termios", "pwd", "objc", "AppKit", "CoreAudio"} & set(result["asked"])


def test_macos_loads_no_windows_module():
    result = probe("darwin")
    assert result["backends"] == ["virtual-device", "process-tap"]
    assert loaded(result["modules"], WINDOWS_ONLY) == []
    assert not {"msvcrt", "winreg", "_winapi", "comtypes", "pycaw", "psutil"} & set(result["asked"])
