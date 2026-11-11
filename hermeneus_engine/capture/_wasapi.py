"""WASAPI process loopback through comtypes. Windows only; imported only by the capture worker.

Nothing here runs on import. ``WasapiHAL()`` loads comtypes, pycaw and psutil
and must be created on a thread that will use COM in the multithreaded
apartment: the worker sets ``sys.coinit_flags`` to ``COINIT_MULTITHREADED``
before it imports this module's dependencies (comtypes initializes COM on the
importing thread with ``sys.coinit_flags``, comtypes/__init__.py:146-161).

Activation follows Microsoft's Application Loopback sample
(Windows-classic-samples/Samples/ApplicationLoopback/cpp/LoopbackCapture.cpp):

1. ``AUDIOCLIENT_ACTIVATION_PARAMS`` with ``ActivationType =
   AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`` and
   ``ProcessLoopbackParams = {TargetProcessId, ProcessLoopbackMode}``, passed
   as a ``VT_BLOB`` ``PROPVARIANT``.
2. ``ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
   IID_IAudioClient, ...)`` with a completion handler that is agile (it
   answers ``IAgileObject``), because Windows calls it from an MTA worker
   thread (mmdeviceapi ActivateAudioInterfaceAsync, Remarks).
3. ``IAudioClient::Initialize`` in shared loopback mode with event callback
   and ``AUTOCONVERTPCM``, on a format chosen by the caller, because process
   loopback has no mix format to ask for.
4. ``IAudioCaptureClient`` ``GetNextPacketSize`` / ``GetBuffer`` /
   ``ReleaseBuffer`` after each event.

Interface layouts (vtable order and IIDs) are copied from the Windows SDK
headers named next to each definition. pycaw supplies ``IAudioClient``,
``WAVEFORMATEX`` and the session enumeration it has carried since 2016.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import POINTER, byref, c_int32, c_ubyte, c_uint32, c_uint64, c_ushort, c_void_p, c_wchar_p
from dataclasses import dataclass
from typing import Any, Optional

log = logging.getLogger(__name__)

# audioclientactivationparams.h
VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
# AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS: "Minimum supported client Windows 10 Build 20348"
# (learn.microsoft.com .../ns-audioclientactivationparams-audioclient_process_loopback_params).
MIN_BUILD = 20348

# audiosessiontypes.h / audioclient.h
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY = 0x1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDIO_SESSION_STATE_ACTIVE = 1  # AudioSessionStateActive

# mmreg.h
WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3

VT_BLOB = 65
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
REFTIMES_PER_SEC = 10_000_000

# Asked of AUTOCONVERTPCM in this order. The first is the usual engine mix
# format; the last is the format of Microsoft's sample.
CAPTURE_FORMATS = (
    (WAVE_FORMAT_IEEE_FLOAT, 48000, 2, 32),
    (WAVE_FORMAT_PCM, 48000, 2, 16),
    (WAVE_FORMAT_PCM, 44100, 2, 16),
)
# WASAPI buffer. Large enough to ride out a busy interpreter; it adds no
# latency because the pump reads each packet as soon as its event fires.
BUFFER_DURATION_S = 0.2
ACTIVATE_TIMEOUT_S = 10.0


class WasapiError(RuntimeError):
    """A WASAPI call failed. The message names the step and the HRESULT."""


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    name: str          # executable name, lower case, for example "chrome.exe"
    create_time: float


@dataclass(frozen=True)
class SessionInfo:
    pid: int           # 0 for the system sounds session
    active: bool
    display_name: str


@dataclass(frozen=True)
class Packet:
    data: bytes        # interleaved samples in the stream format; empty when ``silent``
    frames: int
    flags: int
    qpc_100ns: int     # performance counter at the first frame, in 100 ns units; 0 when unknown

    @property
    def silent(self) -> bool:
        return bool(self.flags & AUDCLNT_BUFFERFLAGS_SILENT)

    @property
    def discontinuity(self) -> bool:
        return bool(self.flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY)


def _hr(exc: BaseException) -> str:
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None):
        code = exc.args[0] if isinstance(exc.args[0], int) else None
    return f"0x{code & 0xFFFFFFFF:08X}" if isinstance(code, int) else str(exc)


def _interfaces():
    """COM interface classes, built once comtypes is importable."""
    import comtypes
    from ctypes import HRESULT  # type: ignore[attr-defined]  # ctypes defines HRESULT only on Windows

    from comtypes import COMMETHOD, GUID, IUnknown

    class IAgileObject(IUnknown):
        # objidl.h; a marker interface with no methods of its own
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_ = []

    class IActivateAudioInterfaceAsyncOperation(IUnknown):
        # mmdeviceapi.h
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetActivateResult",
                      (["out"], POINTER(c_int32), "activateResult"),
                      (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
        ]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):
        # mmdeviceapi.h
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [
            COMMETHOD([], HRESULT, "ActivateCompleted",
                      (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
        ]

    class IAudioCaptureClient(IUnknown):
        # audioclient.h
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer",
                      (["out"], POINTER(POINTER(c_ubyte)), "ppData"),
                      (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                      (["out"], POINTER(c_uint32), "pdwFlags"),
                      (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                      (["out"], POINTER(c_uint64), "pu64QPCPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize", (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
        ]

    return comtypes, IAgileObject, IActivateAudioInterfaceAsyncOperation, \
        IActivateAudioInterfaceCompletionHandler, IAudioCaptureClient


class _LoopbackParams(ctypes.Structure):
    # AUDIOCLIENT_ACTIVATION_PARAMS: ActivationType, then the union whose only
    # member is AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {TargetProcessId, ProcessLoopbackMode}.
    _fields_ = [("ActivationType", c_uint32), ("TargetProcessId", c_uint32), ("ProcessLoopbackMode", c_uint32)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", c_void_p)]


class _PropVariantData(ctypes.Union):
    _fields_ = [("blob", _Blob), ("_pad", c_void_p * 2)]


class _PropVariant(ctypes.Structure):
    _fields_ = [("vt", c_ushort), ("wReserved1", c_ushort), ("wReserved2", c_ushort), ("wReserved3", c_ushort),
                ("data", _PropVariantData)]


class LoopbackStream:
    """One activated, initialized and started process loopback client."""

    def __init__(self, client: Any, capture: Any, event: int, kernel32: Any, fmt: tuple) -> None:
        self._client: Any = client
        self._capture: Any = capture
        self._event = event
        self._k32 = kernel32
        tag, rate, channels, bits = fmt
        self.sample_rate = rate
        self.channels = channels
        self.sample_format = "float32" if tag == WAVE_FORMAT_IEEE_FLOAT else "int16"
        self.block_align = channels * bits // 8
        self._started = False

    def start(self) -> None:
        self._client.Start()
        self._started = True

    def wait(self, timeout_s: float) -> bool:
        return self._k32.WaitForSingleObject(self._event, int(timeout_s * 1000)) == WAIT_OBJECT_0

    def read(self) -> list[Packet]:
        """Every packet that is ready now. Raises WasapiError when the stream is gone."""
        packets = []
        try:
            while self._capture.GetNextPacketSize() > 0:
                data, frames, flags, _dev, qpc = self._capture.GetBuffer()
                try:
                    payload = b"" if flags & AUDCLNT_BUFFERFLAGS_SILENT or not frames else \
                        ctypes.string_at(data, frames * self.block_align)
                finally:
                    self._capture.ReleaseBuffer(frames)
                packets.append(Packet(payload, int(frames), int(flags), int(qpc)))
        except Exception as exc:  # comtypes.COMError or OSError, both carrying an HRESULT
            raise WasapiError(f"reading the loopback stream failed ({_hr(exc)})") from exc
        return packets

    def close(self) -> list[str]:
        failures = []
        if self._started:
            try:
                self._client.Stop()
            except Exception as exc:
                failures.append(f"stop audio client: {_hr(exc)}")
            self._started = False
        if self._event:
            if not self._k32.CloseHandle(self._event):
                failures.append("close event handle")
            self._event = 0
        self._capture = None
        self._client = None
        return failures


class WasapiHAL:
    """The real Windows side of ``_loopback_engine``. Create it on the worker thread that uses it."""

    def __init__(self) -> None:
        import psutil
        from pycaw.api.audioclient import IAudioClient
        from pycaw.api.audioclient.depend import WAVEFORMATEX
        from pycaw.utils import AudioUtilities

        (self._comtypes, self._IAgile, self._IOp, self._IHandler,
         self._ICapture) = _interfaces()
        self._psutil = psutil
        self._IAudioClient = IAudioClient
        self._WAVEFORMATEX = WAVEFORMATEX
        self._utils = AudioUtilities
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        self._k32.CreateEventW.restype = c_void_p
        self._k32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, c_wchar_p]
        self._k32.WaitForSingleObject.restype = c_uint32
        self._k32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
        self._k32.CloseHandle.argtypes = [c_void_p]
        self._k32.QueryPerformanceCounter.argtypes = [POINTER(ctypes.c_int64)]
        self._k32.QueryPerformanceFrequency.argtypes = [POINTER(ctypes.c_int64)]
        freq = ctypes.c_int64(0)
        self._k32.QueryPerformanceFrequency(byref(freq))
        self._qpc_freq = freq.value or 1
        mmdevapi = ctypes.WinDLL("Mmdevapi")  # type: ignore[attr-defined]
        fn = mmdevapi.ActivateAudioInterfaceAsync
        fn.restype = c_int32  # HRESULT, checked by hand so the code is in the message
        fn.argtypes = [c_wchar_p, c_void_p, c_void_p, c_void_p, c_void_p]
        self._activate = fn
        self._handler_cls = self._make_handler_class()

    # ------------------------------------------------------------- threads

    def thread_enter(self) -> None:
        """Join the multithreaded apartment on a thread other than the one that imported comtypes."""
        self._comtypes.CoInitializeEx(0)  # COINIT_MULTITHREADED

    def thread_leave(self) -> None:
        self._comtypes.CoUninitialize()

    # ------------------------------------------------------------- facts

    @staticmethod
    def windows_build() -> int:
        import sys
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]

    def qpc_now_100ns(self) -> int:
        now = ctypes.c_int64(0)
        self._k32.QueryPerformanceCounter(byref(now))
        return now.value * REFTIMES_PER_SEC // self._qpc_freq

    def processes(self) -> list[ProcInfo]:
        out = []
        for p in self._psutil.process_iter(["pid", "ppid", "name", "create_time"]):
            info = p.info
            out.append(ProcInfo(int(info["pid"]), int(info.get("ppid") or 0), str(info.get("name") or "").lower(),
                                float(info.get("create_time") or 0.0)))
        return out

    def sessions(self) -> list[SessionInfo]:
        """Audio sessions on the default render endpoint (IAudioSessionManager2::GetSessionEnumerator)."""
        out = []
        for s in self._utils.GetAllSessions():
            try:
                out.append(SessionInfo(int(s.ProcessId), int(s.State) == AUDIO_SESSION_STATE_ACTIVE,
                                       str(s.DisplayName or "")))
            except Exception:  # the session expired while we read it
                continue
        return out

    # ------------------------------------------------------------- activation

    def _make_handler_class(self):
        from comtypes import COMObject

        IHandler, IAgile = self._IHandler, self._IAgile
        IAudioClient = self._IAudioClient
        import threading

        class Handler(COMObject):
            _com_interfaces_ = [IHandler, IAgile]

            def __init__(self) -> None:
                super().__init__()
                self.done = threading.Event()
                self.client = None
                self.error = ""

            def ActivateCompleted(self, operation):  # called by Windows on an MTA thread
                try:
                    hr, unknown = operation.GetActivateResult()
                    if hr < 0 or not unknown:
                        self.error = f"activation returned 0x{hr & 0xFFFFFFFF:08X}"
                    else:
                        self.client = unknown.QueryInterface(IAudioClient)
                except Exception as exc:
                    self.error = f"activation result could not be read ({_hr(exc)})"
                finally:
                    self.done.set()

        return Handler

    def open_loopback(self, target_pid: int, include_tree: bool) -> LoopbackStream:
        """Activate, initialize and return a stream (not started). Raises WasapiError."""
        mode = (PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE if include_tree
                else PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE)
        params = _LoopbackParams(AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK, int(target_pid), mode)
        prop = _PropVariant()
        prop.vt = VT_BLOB
        prop.data.blob.cbSize = ctypes.sizeof(params)
        prop.data.blob.pBlobData = ctypes.addressof(params)
        handler = self._handler_cls()
        handler_ptr = handler.QueryInterface(self._IHandler)
        operation = POINTER(self._IOp)()
        iid = self._IAudioClient._iid_
        hr = self._activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(iid), byref(prop), handler_ptr,
                            byref(operation))
        if hr < 0:
            raise WasapiError(f"ActivateAudioInterfaceAsync failed (0x{hr & 0xFFFFFFFF:08X})")
        # Windows keeps the handler until the operation is released; both stay
        # referenced here until the callback has run (mmdeviceapi Remarks).
        if not handler.done.wait(ACTIVATE_TIMEOUT_S):
            raise WasapiError(f"process loopback activation did not finish within {ACTIVATE_TIMEOUT_S:.0f} s")
        del operation, handler_ptr
        if handler.client is None:
            raise WasapiError(f"process loopback activation failed: {handler.error}")
        client = handler.client
        fmt, last = self._initialize(client)
        if fmt is None:
            raise WasapiError(f"IAudioClient::Initialize refused every capture format ({last})")
        try:
            unknown = client.GetService(byref(self._ICapture._iid_))
            capture = unknown.QueryInterface(self._ICapture)
        except Exception as exc:
            raise WasapiError(f"IAudioClient::GetService(IAudioCaptureClient) failed ({_hr(exc)})") from exc
        event = self._k32.CreateEventW(None, 0, 0, None)
        if not event:
            raise WasapiError(f"CreateEventW failed (error {ctypes.get_last_error()})")  # type: ignore[attr-defined]
        try:
            client.SetEventHandle(event)
        except Exception as exc:
            self._k32.CloseHandle(event)
            raise WasapiError(f"IAudioClient::SetEventHandle failed ({_hr(exc)})") from exc
        return LoopbackStream(client, capture, event, self._k32, fmt)

    def _initialize(self, client) -> tuple[Optional[tuple], str]:
        flags = (AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                 | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
        last = ""
        for fmt in CAPTURE_FORMATS:
            tag, rate, channels, bits = fmt
            wf = self._WAVEFORMATEX()
            wf.wFormatTag, wf.nChannels, wf.nSamplesPerSec, wf.wBitsPerSample = tag, channels, rate, bits
            wf.nBlockAlign = channels * bits // 8
            wf.nAvgBytesPerSec = rate * wf.nBlockAlign
            wf.cbSize = 0
            try:
                client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, int(BUFFER_DURATION_S * REFTIMES_PER_SEC), 0,
                                  byref(wf), None)
                return fmt, ""
            except Exception as exc:
                last = _hr(exc)
                log.info("process loopback: format %s refused (%s)", fmt, last)
        return None, last
