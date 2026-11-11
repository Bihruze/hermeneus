"""The session lock on Windows: a byte-range lock through ``msvcrt.locking``.

``flock`` does not exist on Windows. ``msvcrt.locking`` locks a byte range
from the current file position and "may continue beyond the end of the file"
(docs.python.org/3/library/msvcrt.html). Windows byte-range locks are
mandatory: another handle cannot read a locked range (LockFile, Remarks). The
lock therefore sits on one byte far past the JSON the file holds, so
``lock_holder`` in another process can still read who owns the session.

"If a process terminates with a portion of a file locked ... the locks are
unlocked by the operating system. However, the time it takes ... depends upon
available system resources" (learn.microsoft.com, LockFile, Remarks). A start
right after a crash may therefore see the session as busy for a moment;
``lock_holder`` reports ``pid_alive`` so the message can say the owner is gone.
"""

from __future__ import annotations

import errno
import os
from typing import Any, Optional

LOCK_OFFSET = 1 << 30
LOCK_BYTES = 1
BUSY_ERRNOS = frozenset({errno.EACCES, getattr(errno, "EDEADLOCK", errno.EDEADLK), errno.EDEADLK})


def _msvcrt(module: Optional[Any]) -> Any:
    if module is not None:
        return module
    import msvcrt  # noqa: PLC0415  (Windows only)
    return msvcrt


def try_lock(fd: int, msvcrt: Optional[Any] = None) -> bool:
    """Take the lock without waiting. False when another handle holds it."""
    m = _msvcrt(msvcrt)
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    try:
        m.locking(fd, m.LK_NBLCK, LOCK_BYTES)
    except OSError as exc:
        if exc.errno in BUSY_ERRNOS:
            return False
        raise
    return True


def unlock(fd: int, msvcrt: Optional[Any] = None) -> None:
    m = _msvcrt(msvcrt)
    os.lseek(fd, LOCK_OFFSET, os.SEEK_SET)
    m.locking(fd, m.LK_UNLCK, LOCK_BYTES)


def open_flags() -> int:
    return os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0)
