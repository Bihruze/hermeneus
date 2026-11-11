"""Give the calling thread the macOS time-constraint scheduling policy (the policy of audio IO threads).

The worker's converter thread holds the GIL while it converts a buffer. At
normal priority, under machine load, the kernel can preempt it while it holds
the GIL, and the HAL IO thread then waits for the GIL inside the IOProc: a
priority inversion. W20 measured it: with the converter at time-constraint
priority there were 0 HAL gaps in 4 runs against 3 at baseline, IOProc
lateness max 1.0 to 1.6 ms against 3.8 to 76 ms, and converter p99 0.64 ms
against 2.4 to 5.9 ms.
"""

from __future__ import annotations

import ctypes
import sys

THREAD_TIME_CONSTRAINT_POLICY = 2
THREAD_TIME_CONSTRAINT_POLICY_COUNT = 4


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _TimeConstraint(ctypes.Structure):
    _fields_ = [("period", ctypes.c_uint32), ("computation", ctypes.c_uint32),
                ("constraint", ctypes.c_uint32), ("preemptible", ctypes.c_int)]


def promote_time_constraint(period_ns: int, computation_ns: int, constraint_ns: int) -> str:
    """Returns "time_constraint" on success, otherwise why the thread keeps its default policy."""
    if sys.platform != "darwin":
        return "default (not macOS)"
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        tb = _Timebase()
        lib.mach_timebase_info(ctypes.byref(tb))

        def to_abs(ns: int) -> int:
            return int(ns) * tb.denom // tb.numer

        lib.mach_thread_self.restype = ctypes.c_uint32
        port = lib.mach_thread_self()
        policy = _TimeConstraint(to_abs(period_ns), to_abs(computation_ns), to_abs(constraint_ns), 1)
        rc = lib.thread_policy_set(port, THREAD_TIME_CONSTRAINT_POLICY, ctypes.byref(policy),
                                   THREAD_TIME_CONSTRAINT_POLICY_COUNT)
        # mach_thread_self() hands out a send right; give it back.
        task = ctypes.c_uint32.in_dll(lib, "mach_task_self_").value
        lib.mach_port_deallocate(task, port)
    except (OSError, AttributeError, ValueError) as exc:
        return f"default ({exc})"
    return "time_constraint" if rc == 0 else f"default (thread_policy_set returned {rc})"
