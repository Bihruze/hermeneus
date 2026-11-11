"""The Python search path a capture worker needs: this package plus the parent's ``sys.path``.

Hermes puts a plugin's dependencies on ``sys.path`` at runtime, from an
environment the bare interpreter does not know, so a worker started with only
the package root cannot import them (measured on macOS for the process tap
worker, commit fd4b69e, process_tap.py ``worker_env``). This is the same rule as
a function the Windows worker uses; the macOS module keeps its own copy so its
bytes stay unchanged, and tests/test_process_loopback.py checks the two agree.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Iterable, Optional

PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def worker_pythonpath(path: Optional[Iterable[str]] = None, *, root: Path = PACKAGE_ROOT,
                      isdir=os.path.isdir) -> str:
    """``PYTHONPATH`` for a worker: the package root first, then each existing ``sys.path`` directory once."""
    dirs = [str(root)]
    for p in (sys.path if path is None else path):
        if p and p not in dirs and isdir(p):
            dirs.append(p)
    return os.pathsep.join(dirs)
