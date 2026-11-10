"""Read-only CoreAudio queries used by feedback routing.

``system_profiler SPAudioDataType`` lists devices and their transport, but it
does not report which devices an aggregate or Multi-Output device contains.
CoreAudio exposes that through ``kAudioAggregateDevicePropertyFullSubDeviceList``
(``AudioHardware.h``), so this module reads it with ctypes. Every call here is
a property get; nothing is created, changed or played.

Selectors used (values from the macOS SDK headers):

* ``kAudioHardwarePropertyDevices`` = ``'dev#'`` (AudioHardware.h)
* ``kAudioObjectPropertyName`` = ``'lnam'`` (AudioHardwareBase.h)
* ``kAudioDevicePropertyDeviceUID`` = ``'uid '`` (AudioHardwareBase.h)
* ``kAudioDevicePropertyTransportType`` = ``'tran'`` (AudioHardwareBase.h)
* ``kAudioAggregateDevicePropertyFullSubDeviceList`` = ``'grup'`` (AudioHardware.h)
* ``kAudioDeviceTransportTypeAggregate`` = ``'grup'``,
  ``kAudioDeviceTransportTypeAutoAggregate`` = ``'fgrp'`` (AudioHardwareBase.h)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct
import sys
from dataclasses import dataclass
from typing import Optional

AGGREGATE_TRANSPORTS = frozenset({"grup", "fgrp"})

_K_SYSTEM_OBJECT = 1
_K_SCOPE_GLOBAL = "glob"
_K_ELEMENT_MAIN = 0
_K_CFSTRING_ENCODING_UTF8 = 0x08000100


@dataclass(frozen=True)
class CoreAudioDevice:
    object_id: int
    name: str
    uid: str
    transport: str                      # four char code such as "bltn", "virt", "grup"
    sub_device_uids: tuple[str, ...]    # empty for non aggregate devices

    @property
    def is_aggregate(self) -> bool:
        return self.transport in AGGREGATE_TRANSPORTS


class CoreAudioUnavailable(RuntimeError):
    """CoreAudio could not be queried on this machine."""


def _fourcc(code: str) -> int:
    return struct.unpack(">I", code.encode("ascii"))[0]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


class _CoreAudio:
    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise CoreAudioUnavailable("CoreAudio exists only on macOS")
        ca_path = ctypes.util.find_library("CoreAudio")
        cf_path = ctypes.util.find_library("CoreFoundation")
        if not ca_path or not cf_path:
            raise CoreAudioUnavailable("CoreAudio or CoreFoundation framework not found")
        self.ca = ctypes.CDLL(ca_path)
        self.cf = ctypes.CDLL(cf_path)
        self.ca.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32)]
        self.ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
        self.ca.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        self.ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        self.cf.CFArrayGetCount.restype = ctypes.c_long
        self.cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        self.cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]

    def _get(self, obj: int, selector: str) -> Optional[bytes]:
        addr = _Address(_fourcc(selector), _fourcc(_K_SCOPE_GLOBAL), _K_ELEMENT_MAIN)
        size = ctypes.c_uint32(0)
        if self.ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size)) != 0:
            return None
        if size.value == 0:
            return b""
        buf = ctypes.create_string_buffer(size.value)
        if self.ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf) != 0:
            return None
        return buf.raw[: size.value]

    def _cfstring_to_str(self, ref: int) -> str:
        buf = ctypes.create_string_buffer(1024)
        if not self.cf.CFStringGetCString(ref, buf, len(buf), _K_CFSTRING_ENCODING_UTF8):
            return ""
        return buf.value.decode("utf-8", errors="replace")

    def _get_cfstring(self, obj: int, selector: str) -> str:
        raw = self._get(obj, selector)
        if not raw:
            return ""
        ref = struct.unpack("P", raw)[0]
        if not ref:
            return ""
        try:
            return self._cfstring_to_str(ref)
        finally:
            self.cf.CFRelease(ref)

    def _get_uid_array(self, obj: int, selector: str) -> tuple[str, ...]:
        raw = self._get(obj, selector)
        if not raw:
            return ()
        ref = struct.unpack("P", raw)[0]
        if not ref:
            return ()
        try:
            count = self.cf.CFArrayGetCount(ref)
            return tuple(self._cfstring_to_str(self.cf.CFArrayGetValueAtIndex(ref, i)) for i in range(count))
        finally:
            self.cf.CFRelease(ref)

    def devices(self) -> list[CoreAudioDevice]:
        raw = self._get(_K_SYSTEM_OBJECT, "dev#")
        if raw is None:
            raise CoreAudioUnavailable("kAudioHardwarePropertyDevices query failed")
        ids = struct.unpack(f"{len(raw) // 4}I", raw)
        out = []
        for obj in ids:
            tran_raw = self._get(obj, "tran")
            transport = ""
            if tran_raw and len(tran_raw) >= 4:
                transport = struct.pack(">I", struct.unpack("I", tran_raw[:4])[0]).decode("ascii", errors="replace")
            subs: tuple[str, ...] = ()
            if transport in AGGREGATE_TRANSPORTS:
                subs = self._get_uid_array(obj, "grup")
            out.append(CoreAudioDevice(object_id=obj, name=self._get_cfstring(obj, "lnam"),
                                       uid=self._get_cfstring(obj, "uid "), transport=transport,
                                       sub_device_uids=subs))
        return out


def list_coreaudio_devices() -> list[CoreAudioDevice]:
    """All CoreAudio devices with transport type and, for aggregates, member UIDs.

    Raises CoreAudioUnavailable when CoreAudio cannot be queried.
    """
    return _CoreAudio().devices()
