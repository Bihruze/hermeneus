"""Feedback-loop checks for spoken output, plus the capture gate.

Spoken translation must not reach the device we capture from, or the engine
would transcribe and translate its own voice. This module answers one
question before playback starts: can audio written to the chosen output
device come back through the capture source?

Device facts come from three read-only sources:

* ``system_profiler SPAudioDataType -json`` for names, transport type and
  channel counts.
* CoreAudio (``coreaudio_devices``) for the member list of aggregate and
  Multi-Output devices, which system_profiler does not report.
* ``sounddevice.query_devices()`` for the names PortAudio will open.

When a risky route is chosen on purpose, ``CaptureGate`` zeroes capture
frames while spoken output is audible plus a tail (see ``DEFAULT_GATE_TAIL_S``).
"""

from __future__ import annotations

import collections
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Sequence

from ..contracts import ENGINE_CHANNELS, ENGINE_SAMPLE_RATE, ENGINE_SAMPLE_WIDTH, FeedbackRiskError, FrameSink, OutputDevice
from .coreaudio_devices import CoreAudioDevice, CoreAudioUnavailable, list_coreaudio_devices

BACKEND_VIRTUAL_DEVICE = "virtual-device"
BACKEND_PROCESS_TAP = "process-tap"

SYSTEM_PROFILER = "/usr/sbin/system_profiler"

# Gate tail. The gate already shifts each played block to the time PortAudio
# says it reaches the DAC (outputBufferDacTime), so the tail only has to cover
# what PortAudio cannot see on a loopback route:
#   * the Multi-Output Device drift-correction buffer of secondary members,
#   * one capture chunk of timestamp granularity (the sink stamps the first
#     sample of a chunk, so up to one chunk of the loop can carry an earlier
#     stamp than the audio it holds; capture backends deliver 10 to 100 ms),
#   * scheduling jitter of the capture thread.
# 250 ms covers a 100 ms chunk plus 150 ms for the other two. The value is a
# design bound, not a measurement on a Multi-Output route (this Mac has none);
# gate G3 measures the real loop delay and this constant follows that number.
DEFAULT_GATE_TAIL_S = 0.25
# Lead margin before the first audible sample, for clock skew between the
# playback and capture threads.
DEFAULT_GATE_LEAD_S = 0.02

_SP_TRANSPORT_PREFIX = "coreaudio_device_type_"
_FOURCC_TRANSPORT = {
    "bltn": "builtin", "virt": "virtual", "grup": "aggregate", "fgrp": "aggregate",
    "usb ": "usb", "blue": "bluetooth", "blea": "bluetooth", "airp": "airplay",
    "hdmi": "hdmi", "dprt": "displayport", "thun": "thunderbolt", "pci ": "pci",
    "fwir": "firewire", "cntn": "continuity",
}


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioDeviceInfo:
    name: str
    transport: str = "unknown"            # builtin, virtual, aggregate, usb, bluetooth, ...
    uid: str = ""
    input_channels: int = 0
    output_channels: int = 0
    is_default_output: bool = False
    # Names of member devices for an aggregate. None means the device is an
    # aggregate whose members could not be read.
    members: Optional[tuple[str, ...]] = ()

    @property
    def is_aggregate(self) -> bool:
        return self.transport == "aggregate"


@dataclass(frozen=True)
class AudioInventory:
    devices: tuple[AudioDeviceInfo, ...]
    notes: tuple[str, ...] = ()

    def find(self, name: Optional[str]) -> Optional[AudioDeviceInfo]:
        if not name:
            return None
        for d in self.devices:
            if d.name == name:
                return d
        folded = name.casefold()
        matches = [d for d in self.devices if d.name.casefold() == folded]
        return matches[0] if len(matches) == 1 else None

    def outputs(self) -> list[AudioDeviceInfo]:
        return [d for d in self.devices if d.output_channels > 0]


def run_system_profiler(timeout_s: float = 15.0) -> dict:
    """Run ``system_profiler SPAudioDataType -json`` (read-only) and parse it."""
    proc = subprocess.run([SYSTEM_PROFILER, "SPAudioDataType", "-json"], capture_output=True,
                          timeout=timeout_s, check=True)
    return json.loads(proc.stdout)


def _system_profiler_entries(data: dict) -> list[dict]:
    out: list[dict] = []

    def walk(items: Iterable[dict]) -> None:
        for it in items:
            if not isinstance(it, dict):
                continue
            if any(k.startswith("coreaudio_device_") for k in it):
                out.append(it)
            walk(it.get("_items", ()))

    walk(data.get("SPAudioDataType", ()))
    return out


def _sp_transport(entry: dict) -> str:
    raw = str(entry.get("coreaudio_device_transport", ""))
    if raw.startswith(_SP_TRANSPORT_PREFIX):
        raw = raw[len(_SP_TRANSPORT_PREFIX):]
    if raw == "autoaggregate":
        raw = "aggregate"
    return raw or "unknown"


def build_inventory(system_profiler_json: Optional[dict], coreaudio: Optional[Sequence[CoreAudioDevice]],
                    sd_devices: Optional[Sequence[dict]], notes: Sequence[str] = ()) -> AudioInventory:
    """Merge the three sources. Any source may be None when it could not be read."""
    notes = list(notes)
    by_name: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return by_name.setdefault(name, {"name": name})

    for entry in _system_profiler_entries(system_profiler_json or {}):
        name = entry.get("_name")
        if not name:
            continue
        s = slot(name)
        s["transport"] = _sp_transport(entry)
        s["input_channels"] = int(entry.get("coreaudio_device_input", 0) or 0)
        s["output_channels"] = int(entry.get("coreaudio_device_output", 0) or 0)
        s["is_default_output"] = entry.get("coreaudio_default_audio_output_device") == "spaudio_yes"

    uid_to_name = {d.uid: d.name for d in coreaudio or () if d.uid}
    for d in coreaudio or ():
        s = slot(d.name)
        s["uid"] = d.uid
        mapped = _FOURCC_TRANSPORT.get(d.transport)
        if mapped and (mapped == "aggregate" or s.get("transport", "unknown") == "unknown"):
            s["transport"] = mapped
        if d.is_aggregate:
            s["members"] = tuple(uid_to_name.get(u, u) for u in d.sub_device_uids)

    for d in sd_devices or ():
        name = d.get("name")
        if not name:
            continue
        s = slot(name)
        s.setdefault("input_channels", int(d.get("max_input_channels", 0)))
        s.setdefault("output_channels", int(d.get("max_output_channels", 0)))
        if not s.get("input_channels"):
            s["input_channels"] = int(d.get("max_input_channels", 0))
        if not s.get("output_channels"):
            s["output_channels"] = int(d.get("max_output_channels", 0))

    devices = []
    for s in by_name.values():
        transport = s.get("transport", "unknown")
        if transport == "unknown" and _looks_aggregate(s["name"]):
            transport = "aggregate"
        members = s.get("members", ())
        if transport == "aggregate" and "members" not in s:
            members = None
        devices.append(AudioDeviceInfo(name=s["name"], transport=transport, uid=s.get("uid", ""),
                                       input_channels=s.get("input_channels", 0),
                                       output_channels=s.get("output_channels", 0),
                                       is_default_output=s.get("is_default_output", False),
                                       members=members))
    return AudioInventory(devices=tuple(devices), notes=tuple(notes))


def _looks_aggregate(name: str) -> bool:
    # Audio MIDI Setup names new devices "Multi-Output Device" and "Aggregate Device".
    folded = name.casefold()
    return "multi-output" in folded or "aggregate" in folded


def load_inventory() -> AudioInventory:
    """Read the live device inventory. Read-only; each failed source becomes a note."""
    notes: list[str] = []
    sp = None
    try:
        sp = run_system_profiler()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        notes.append(f"system_profiler unavailable: {type(exc).__name__}")
    ca = None
    try:
        ca = list_coreaudio_devices()
    except (CoreAudioUnavailable, OSError) as exc:
        notes.append(f"CoreAudio query unavailable: {exc}")
    sdl = None
    try:
        import sounddevice as sd  # noqa: PLC0415  (optional at import time)
        sdl = [dict(d) for d in sd.query_devices()]
    except Exception as exc:  # PortAudio missing or failing to initialise
        notes.append(f"sounddevice unavailable: {type(exc).__name__}")
    return build_inventory(sp, ca, sdl, notes)


# --------------------------------------------------------------------------
# Verdict
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RouteVerdict:
    risky: bool
    code: str            # see the CODE_* constants
    explanation: str     # plain language, shown to the user
    output_device: str
    capture_device: Optional[str]
    capture_backend: str
    warnings: tuple[str, ...] = ()


CODE_OK = "ok"
CODE_SAME_DEVICE = "same-device"
CODE_OUTPUT_AGGREGATE_HAS_CAPTURE = "output-aggregate-contains-capture"
CODE_CAPTURE_AGGREGATE_HAS_OUTPUT = "capture-aggregate-contains-output"
CODE_AGGREGATE_MEMBERS_UNKNOWN = "aggregate-members-unknown"
CODE_OUTPUT_UNKNOWN = "output-device-unknown"
CODE_OWN_PROCESS_CAPTURED = "own-process-captured"


def _expand(dev: Optional[AudioDeviceInfo], inv: AudioInventory, depth: int = 0) -> Optional[set[str]]:
    """Names a device writes to or reads from, following nested aggregates. None if unknown."""
    if dev is None:
        return set()
    names = {dev.name}
    if not dev.is_aggregate:
        return names
    if dev.members is None or depth > 4:
        return None
    for m in dev.members:
        sub = inv.find(m)
        if sub is None:
            names.add(m)
            continue
        inner = _expand(sub, inv, depth + 1)
        if inner is None:
            return None
        names |= inner
    return names


def check_feedback_risk(output_device_name: str, capture_device_name: Optional[str], *,
                        capture_backend: str = BACKEND_VIRTUAL_DEVICE,
                        own_process_excluded: bool = True,
                        inventory: Optional[AudioInventory] = None) -> RouteVerdict:
    """Decide whether playing to ``output_device_name`` can be re-captured.

    ``capture_backend`` names the active capture backend. For
    ``virtual-device`` the capture source is an input device and the check
    compares devices. For ``process-tap`` capture follows processes, not
    devices, so the only loop is our own process being tapped;
    ``own_process_excluded`` states whether the tap excludes this PID.
    """
    inv = inventory if inventory is not None else load_inventory()
    out = inv.find(output_device_name)
    warnings: list[str] = []

    def verdict(risky: bool, code: str, text: str) -> RouteVerdict:
        return RouteVerdict(risky=risky, code=code, explanation=text, output_device=output_device_name,
                            capture_device=capture_device_name, capture_backend=capture_backend,
                            warnings=tuple(warnings))

    if out is not None and out.transport == "virtual":
        warnings.append(f"'{out.name}' is a virtual device. You will not hear spoken output unless "
                        "another app plays that device to your speakers or headphones.")

    if capture_backend == BACKEND_PROCESS_TAP:
        if not own_process_excluded:
            return verdict(True, CODE_OWN_PROCESS_CAPTURED,
                           "The app capture includes this process, so spoken translation would be "
                           "captured and translated again.")
        return verdict(False, CODE_OK, "App capture excludes this process, so spoken translation "
                                       "cannot be captured again on any output device.")

    if out is None:
        return verdict(True, CODE_OUTPUT_UNKNOWN,
                       f"The output device '{output_device_name}' was not found, so it cannot be "
                       "checked for a feedback loop.")

    cap = inv.find(capture_device_name)
    if capture_device_name and cap is None:
        warnings.append(f"Capture device '{capture_device_name}' was not found in the device list.")
        cap = AudioDeviceInfo(name=capture_device_name)

    if cap is not None and out.name == cap.name:
        return verdict(True, CODE_SAME_DEVICE,
                       f"'{out.name}' is the device being captured. Spoken translation played there "
                       "would be captured and translated again.")

    out_names = _expand(out, inv)
    if out_names is None:
        return verdict(True, CODE_AGGREGATE_MEMBERS_UNKNOWN,
                       f"'{out.name}' combines several devices and its member list could not be read, "
                       "so it may include the captured device.")
    if cap is not None and cap.name in out_names:
        return verdict(True, CODE_OUTPUT_AGGREGATE_HAS_CAPTURE,
                       f"'{out.name}' also sends audio to '{cap.name}', which is being captured. "
                       "Spoken translation would be captured and translated again.")

    cap_names = _expand(cap, inv)
    if cap_names is None:
        return verdict(True, CODE_AGGREGATE_MEMBERS_UNKNOWN,
                       f"The captured device '{cap.name}' combines several devices and its member "
                       "list could not be read, so it may include the output device.")
    if cap_names & out_names:
        shared = sorted(cap_names & out_names)[0]
        return verdict(True, CODE_CAPTURE_AGGREGATE_HAS_OUTPUT,
                       f"The captured device '{cap.name}' includes '{shared}', which also receives "
                       "spoken translation. It would be captured and translated again.")

    return verdict(False, CODE_OK, f"Spoken translation on '{out.name}' does not reach "
                                   f"'{cap.name if cap else 'the capture source'}'.")


def ensure_route_safe(v: RouteVerdict, *, gate_mode: bool = False) -> None:
    """Raise FeedbackRiskError for a risky route unless the caller chose gate mode."""
    if v.risky and not gate_mode:
        raise FeedbackRiskError(v.explanation)


def safe_output_devices(capture_device_name: Optional[str], *,
                        capture_backend: str = BACKEND_VIRTUAL_DEVICE,
                        own_process_excluded: bool = True,
                        inventory: Optional[AudioInventory] = None,
                        include_virtual: bool = False) -> list[OutputDevice]:
    """Output devices that pass ``check_feedback_risk``, audible devices first.

    Virtual devices are left out unless ``include_virtual`` is set, because a
    user picking one would hear nothing.
    """
    inv = inventory if inventory is not None else load_inventory()
    result: list[tuple[int, OutputDevice]] = []
    for d in inv.outputs():
        v = check_feedback_risk(d.name, capture_device_name, capture_backend=capture_backend,
                                own_process_excluded=own_process_excluded, inventory=inv)
        if v.risky:
            continue
        if d.transport == "virtual" and not include_virtual:
            continue
        label = d.name if d.transport != "virtual" else f"{d.name} (virtual)"
        rank = 0 if d.is_default_output else (2 if d.transport == "virtual" else 1)
        result.append((rank, OutputDevice(device_id=d.name, label=label)))
    result.sort(key=lambda r: r[0])
    return [r[1] for r in result]


# --------------------------------------------------------------------------
# Capture gate
# --------------------------------------------------------------------------

_BYTES_PER_SECOND = ENGINE_SAMPLE_RATE * ENGINE_SAMPLE_WIDTH * ENGINE_CHANNELS


class CaptureGate:
    """Zero capture frames while spoken output is audible, plus a tail.

    Playback calls ``mark_audible`` from its audio callback with the
    monotonic time span the block will be heard. That call only appends to a
    bounded deque or assigns into a list, so it never blocks. Capture wraps
    its sink with ``wrap``; a chunk whose span overlaps an audible span
    (widened by ``lead_s`` before and ``tail_s`` after) is replaced by
    silence of the same length, so timestamps and stream length stay intact.
    """

    def __init__(self, *, tail_s: float = DEFAULT_GATE_TAIL_S, lead_s: float = DEFAULT_GATE_LEAD_S,
                 clock: Callable[[], int] = time.monotonic_ns) -> None:
        self.tail_ns = int(tail_s * 1e9)
        self.lead_ns = int(lead_s * 1e9)
        self._clock = clock
        self._spans: collections.deque[list[int]] = collections.deque(maxlen=32)
        self.gated_chunks = 0

    def mark_audible(self, start_ns: int, end_ns: int) -> None:
        spans = self._spans
        if spans:
            last = spans[-1]
            if start_ns <= last[1] + self.tail_ns:
                if end_ns > last[1]:
                    last[1] = end_ns
                return
        spans.append([start_ns, end_ns])

    def is_gated(self, start_ns: int, end_ns: int) -> bool:
        for s, e in tuple(self._spans):
            if start_ns <= e + self.tail_ns and end_ns >= s - self.lead_ns:
                return True
        return False

    def wrap(self, sink: FrameSink) -> FrameSink:
        def gated_sink(pcm: bytes, capture_ns: int) -> None:
            end_ns = capture_ns + int(len(pcm) * 1e9 / _BYTES_PER_SECOND)
            if self.is_gated(capture_ns, end_ns):
                self.gated_chunks += 1
                sink(bytes(len(pcm)), capture_ns)
            else:
                sink(pcm, capture_ns)
        return gated_sink
