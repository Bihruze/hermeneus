"""Is a process alive, without signals.

``os.kill(pid, 0)`` is a liveness probe on POSIX, but on Windows CPython maps
signal 0 to ``CTRL_C_EVENT`` and sends it to the target's console group
(Hermes docs, website/docs/user-guide/windows-native.md, "Process management
internals"; bpo-14484). This asks the kernel instead: OpenProcess, then
GetExitCodeProcess, the same calls psutil.pid_exists makes on Windows.

A process that exited with code 259 also reports STILL_ACTIVE ("If a thread
returns STILL_ACTIVE (259) as an error code, then applications that test for
this value could interpret it to mean that the thread is still running",
learn.microsoft.com GetExitCodeProcess, Remarks), so a STILL_ACTIVE answer is
confirmed with a zero-timeout wait on the process handle: a live process is not
signaled.
"""

from __future__ import annotations

import ctypes
from typing import Any, Optional

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259
WAIT_TIMEOUT = 0x102
ERROR_ACCESS_DENIED = 5


def _kernel32() -> Any:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k32.WaitForSingleObject.restype = ctypes.c_uint32
    return k32


def pid_alive(pid: int, kernel32: Optional[Any] = None,
              last_error=lambda: ctypes.get_last_error()) -> bool:  # type: ignore[attr-defined]
    if pid <= 0:
        return False
    k32 = kernel32 or _kernel32()
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, int(pid))
    if not handle:
        # Access denied means the process exists but belongs to someone else.
        return last_error() == ERROR_ACCESS_DENIED
    try:
        code = ctypes.c_uint32(0)
        if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        if code.value != STILL_ACTIVE:
            return False
        return k32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(handle)
