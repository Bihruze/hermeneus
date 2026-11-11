"""Thin PyObjC layer over the Core Audio HAL calls the process tap backend needs.

Everything that touches PyObjC or raw memory lives here, so the backend can be
tested with a fake that has the same methods. Nothing in this module prompts
for permission except ``create_tap`` and ``start`` (macOS asks for System Audio
Recording the first time a tap delivers audio to the responsible app).

PyObjC 13 bridges ``AudioDeviceCreateIOProcIDWithBlock`` but returns the
``AudioDeviceIOProcID`` as an opaque pointer that cannot be passed back to
``AudioDeviceStart``. The functions that take or return an IOProc ID are
therefore re-declared with ``objc.loadBundleFunctions`` so the ID travels as an
unsigned 64-bit integer, which has the same calling convention as a pointer on
arm64 and x86_64. The block receives its pointer arguments as integers too and
reads them with ``ctypes``, which avoids PyObjC struct conversion on the audio
thread.
"""

from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass
from typing import Callable, Optional

_COREAUDIO_PATH = "/System/Library/Frameworks/CoreAudio.framework"

# Called on the HAL IO thread: (sample_time, host_time_ticks, float32 bytes, channels)
IOCallback = Callable[[float, int, bytes, int], None]


class HALError(RuntimeError):
    def __init__(self, what: str, status: int) -> None:
        super().__init__(f"{what} failed with OSStatus {status} ({fourcc(status)})")
        self.what = what
        self.status = status


def fourcc(status: int) -> str:
    raw = struct.pack(">i", status)
    if all(32 <= b < 127 for b in raw):
        return "'" + raw.decode("ascii") + "'"
    return str(status)


@dataclass(frozen=True)
class ProcessInfo:
    object_id: int
    pid: int
    bundle_id: str
    running_output: bool


@dataclass(frozen=True)
class AppInfo:
    name: str
    regular: bool  # NSApplicationActivationPolicyRegular: has a Dock icon


@dataclass(frozen=True)
class TapFormat:
    sample_rate: float
    format_id: int
    format_flags: int
    channels: int
    bits_per_channel: int


@dataclass(frozen=True)
class TapHandle:
    tap_id: int
    uid: str


class _AudioBuffer(ctypes.Structure):
    _fields_ = [("mNumberChannels", ctypes.c_uint32),
                ("mDataByteSize", ctypes.c_uint32),
                ("mData", ctypes.c_void_p)]


class _AudioBufferListHead(ctypes.Structure):
    _fields_ = [("mNumberBuffers", ctypes.c_uint32), ("mBuffers", _AudioBuffer * 1)]


class _AudioTimeStampHead(ctypes.Structure):
    _fields_ = [("mSampleTime", ctypes.c_double), ("mHostTime", ctypes.c_uint64)]


_BUFFER_STRIDE = ctypes.sizeof(_AudioBuffer)
_BUFFERS_OFFSET = _AudioBufferListHead.mBuffers.offset

# kAudioFormatLinearPCM and kAudioFormatFlagIsFloat from CoreAudioBaseTypes.h
FORMAT_LINEAR_PCM = 0x6C70636D
FLAG_IS_FLOAT = 1 << 0
FLAG_IS_NON_INTERLEAVED = 1 << 5


class CoreAudioHAL:
    """Real implementation. Construct only after ``available()`` said yes."""

    def __init__(self) -> None:
        import objc
        import CoreAudio
        from Foundation import NSBundle

        self._objc = objc
        self._ca = CoreAudio
        block_sig = {
            "retval": {"type": b"v"},
            "arguments": {0: {"type": b"^v"}, 1: {"type": b"Q"}, 2: {"type": b"Q"},
                          3: {"type": b"Q"}, 4: {"type": b"Q"}, 5: {"type": b"Q"}},
        }
        fns: dict = {}
        objc.loadBundleFunctions(NSBundle.bundleWithPath_(_COREAUDIO_PATH), fns, [
            ("AudioDeviceCreateIOProcIDWithBlock", b"i^QI@@?", "",
             {"arguments": {0: {"type_modifier": b"o"}, 3: {"callable": block_sig}}}),
            ("AudioDeviceStart", b"iIQ"),
            ("AudioDeviceStop", b"iIQ"),
            ("AudioDeviceDestroyIOProcID", b"iIQ"),
            ("AudioConvertHostTimeToNanos", b"QQ"),
        ])
        self._fn = fns
        self._sys = CoreAudio.kAudioObjectSystemObject

    # ---------------------------------------------------------------- properties

    def _address(self, selector: int):
        ca = self._ca
        return (selector, ca.kAudioObjectPropertyScopeGlobal, ca.kAudioObjectPropertyElementMain)

    def _get(self, obj: int, selector: int, what: str, qualifier: Optional[bytes] = None,
             size: Optional[int] = None) -> bytes:
        ca, objc = self._ca, self._objc
        addr = self._address(selector)
        if size is None:
            status, size = ca.AudioObjectGetPropertyDataSize(
                obj, addr, len(qualifier or b""), qualifier or objc.NULL, None)
            if status:
                raise HALError(what, status)
        if size == 0:
            return b""
        status, _, data = ca.AudioObjectGetPropertyData(
            obj, addr, len(qualifier or b""), qualifier or objc.NULL, size, None)
        if status:
            raise HALError(what, status)
        return bytes(data)

    def _get_u32(self, obj: int, selector: int, what: str) -> int:
        return struct.unpack("<I", self._get(obj, selector, what, size=4))[0]

    def _get_cfstring(self, obj: int, selector: int, what: str) -> str:
        raw = self._get(obj, selector, what, size=8)
        ptr = struct.unpack("<Q", raw)[0]
        if not ptr:
            return ""
        from CoreFoundation import CFRelease

        value = self._objc.objc_object(c_void_p=ptr)
        text = str(value)
        CFRelease(value)  # the HAL returns a retained CFString; PyObjC holds its own reference
        return text

    # ---------------------------------------------------------------- processes

    def process_objects(self) -> list[int]:
        raw = self._get(self._sys, self._ca.kAudioHardwarePropertyProcessObjectList, "read process list")
        return list(struct.unpack(f"<{len(raw) // 4}I", raw))

    def process_info(self, object_id: int) -> ProcessInfo:
        ca = self._ca
        pid = struct.unpack("<i", self._get(object_id, ca.kAudioProcessPropertyPID, "read process pid", size=4))[0]
        bundle = self._get_cfstring(object_id, ca.kAudioProcessPropertyBundleID, "read process bundle id")
        running = self._get_u32(object_id, ca.kAudioProcessPropertyIsRunningOutput, "read process output state")
        return ProcessInfo(object_id=object_id, pid=pid, bundle_id=bundle, running_output=bool(running))

    def process_object_for_pid(self, pid: int) -> int:
        raw = self._get(self._sys, self._ca.kAudioHardwarePropertyTranslatePIDToProcessObject,
                        "translate pid", qualifier=struct.pack("<i", pid), size=4)
        return struct.unpack("<I", raw)[0]

    def app_info(self, pid: int) -> Optional[AppInfo]:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return None
        return AppInfo(name=str(app.localizedName() or ""), regular=int(app.activationPolicy()) == 0)

    # ---------------------------------------------------------------- tap and aggregate

    def _tap_description(self, process_objects: list[int], exclude: bool, name: str, mute: bool = False):
        ca = self._ca
        if exclude:
            desc = ca.CATapDescription.alloc().initMonoGlobalTapButExcludeProcesses_(list(process_objects))
        else:
            desc = ca.CATapDescription.alloc().initMonoMixdownOfProcesses_(list(process_objects))
        desc.setPrivate_(True)
        desc.setName_(name)
        if mute:
            # The tapped processes stay silent on their output device while the tap is read.
            desc.setMuteBehavior_(ca.CATapMutedWhenTapped)
        return desc

    def create_tap(self, process_objects: list[int], exclude: bool, name: str, mute: bool = False) -> TapHandle:
        desc = self._tap_description(process_objects, exclude, name, mute)
        status, tap_id = self._ca.AudioHardwareCreateProcessTap(desc, None)
        if status:
            raise HALError("create process tap", status)
        return TapHandle(tap_id=int(tap_id), uid=str(desc.UUID().UUIDString()))

    def set_tap_processes(self, tap_id: int, process_objects: list[int], exclude: bool, name: str,
                          mute: bool = False) -> None:
        """Change which processes a live tap reads (kAudioTapPropertyDescription is settable)."""
        desc = self._tap_description(process_objects, exclude, name, mute)
        pointer = struct.pack("<Q", self._objc.pyobjc_id(desc))
        status = self._ca.AudioObjectSetPropertyData(
            tap_id, self._address(self._ca.kAudioTapPropertyDescription), 0, self._objc.NULL, len(pointer), pointer)
        if status:
            raise HALError("update process tap", status)

    def tap_format(self, tap_id: int) -> TapFormat:
        raw = self._get(tap_id, self._ca.kAudioTapPropertyFormat, "read tap format", size=40)
        rate, fmt_id, flags, _bpp, _fpp, _bpf, ch, bits, _ = struct.unpack("<dIIIIIIII", raw)
        return TapFormat(sample_rate=rate, format_id=fmt_id, format_flags=flags, channels=ch,
                         bits_per_channel=bits)

    def destroy_tap(self, tap_id: int) -> None:
        status = self._ca.AudioHardwareDestroyProcessTap(tap_id)
        if status:
            raise HALError("destroy process tap", status)

    def create_aggregate(self, tap_uid: str, name: str, uid: str) -> int:
        ca = self._ca

        def key(k) -> str:
            return k.decode() if isinstance(k, bytes) else str(k)

        description = {
            key(ca.kAudioAggregateDeviceNameKey): name,
            key(ca.kAudioAggregateDeviceUIDKey): uid,
            key(ca.kAudioAggregateDeviceIsPrivateKey): True,
            key(ca.kAudioAggregateDeviceIsStackedKey): False,
            key(ca.kAudioAggregateDeviceTapAutoStartKey): True,
            key(ca.kAudioAggregateDeviceTapListKey): [
                {key(ca.kAudioSubTapUIDKey): tap_uid, key(ca.kAudioSubTapDriftCompensationKey): True},
            ],
        }
        status, device_id = ca.AudioHardwareCreateAggregateDevice(description, None)
        if status:
            raise HALError("create aggregate device", status)
        return int(device_id)

    def destroy_aggregate(self, device_id: int) -> None:
        status = self._ca.AudioHardwareDestroyAggregateDevice(device_id)
        if status:
            raise HALError("destroy aggregate device", status)

    # ---------------------------------------------------------------- IO

    def create_ioproc(self, device_id: int, callback: IOCallback) -> tuple[int, object]:
        """Returns (proc_id, block). The caller keeps ``block`` alive until the IOProc is destroyed."""
        string_at = ctypes.string_at
        head = _AudioBufferListHead.from_address
        stamp = _AudioTimeStampHead.from_address
        buffer_at = _AudioBuffer.from_address

        def block(_now, in_data, in_time, _out_data, _out_time):
            if not in_data or not in_time:
                return
            bl = head(in_data)
            ts = stamp(in_time)
            n = bl.mNumberBuffers
            if n == 1:
                b = bl.mBuffers[0]
                data = string_at(b.mData, b.mDataByteSize) if b.mDataByteSize and b.mData else b""
                callback(ts.mSampleTime, ts.mHostTime, data, b.mNumberChannels)
            elif n > 1:
                # Non-interleaved: one buffer per channel. Interleave so the callback sees one layout.
                parts = []
                for i in range(n):
                    b = buffer_at(in_data + _BUFFERS_OFFSET + i * _BUFFER_STRIDE)
                    parts.append(string_at(b.mData, b.mDataByteSize) if b.mDataByteSize and b.mData else b"")
                callback(ts.mSampleTime, ts.mHostTime, _interleave(parts), n)

        status, proc_id = self._fn["AudioDeviceCreateIOProcIDWithBlock"](None, device_id, None, block)
        if status:
            raise HALError("create IOProc", status)
        return int(proc_id), block

    def destroy_ioproc(self, device_id: int, proc_id: int) -> None:
        status = self._fn["AudioDeviceDestroyIOProcID"](device_id, proc_id)
        if status:
            raise HALError("destroy IOProc", status)

    def start(self, device_id: int, proc_id: int) -> None:
        status = self._fn["AudioDeviceStart"](device_id, proc_id)
        if status:
            raise HALError("start aggregate device", status)

    def stop(self, device_id: int, proc_id: int) -> None:
        status = self._fn["AudioDeviceStop"](device_id, proc_id)
        if status:
            raise HALError("stop aggregate device", status)

    def host_time_to_ns(self, host_time: int) -> int:
        return int(self._fn["AudioConvertHostTimeToNanos"](host_time))


def _interleave(parts: list[bytes]) -> bytes:
    import numpy as np

    frames = min(len(p) for p in parts) // 4
    planes = [np.frombuffer(p, dtype="<f4", count=frames) for p in parts]
    return np.stack(planes, axis=1).tobytes()
