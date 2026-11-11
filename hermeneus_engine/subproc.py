"""The one place where Hermeneus starts other processes.

Every helper process gets a minimal environment: a short allowlist of
variables, never the parent's API keys, tokens or other secrets. Programs are
named by absolute path, so nothing is resolved through PATH. Python children
(capture workers, the overlay, the doctor's test player) also get a
PYTHONPATH that holds this package and every directory on the parent's
``sys.path``: Hermes adds a plugin's dependencies to ``sys.path`` at runtime,
from an environment the bare interpreter does not know.

``tests/test_subproc.py`` checks that no other module in the package calls
``subprocess`` directly.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

SYSTEM_PROFILER = "/usr/sbin/system_profiler"
PS = "/bin/ps"

# Variables a helper process may inherit. None of them holds a secret.
ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
# Windows. subprocess docs: "If specified, env must provide any variables required for the
# program to execute. On Windows, in order to run a side-by-side assembly the specified env
# must include a valid SystemRoot." The rest keep temp files, the user profile and the console
# code page behaving as in the parent.
WINDOWS_ENV_ALLOWLIST = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
                         "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "COMSPEC", "NUMBER_OF_PROCESSORS",
                         "PROCESSOR_ARCHITECTURE", "LANG", "PYTHONUTF8", "PYTHONIOENCODING")

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL
TimeoutExpired = subprocess.TimeoutExpired
SubprocessError = subprocess.SubprocessError
CalledProcessError = subprocess.CalledProcessError
Popen = subprocess.Popen  # type for annotations; start processes with popen()


def minimal_env(parent: Optional[dict] = None, *, windows: Optional[bool] = None) -> dict:
    """The allowlisted variables of ``parent`` (default: this process).

    With ``windows`` (default: running on Windows) the Windows allowlist applies and names match
    case-insensitively, as the Windows environment does."""
    parent = os.environ if parent is None else parent
    windows = sys.platform == "win32" if windows is None else windows
    if windows:
        upper = {k.upper(): v for k, v in parent.items()}
        return {k: upper[k] for k in WINDOWS_ENV_ALLOWLIST if k in upper}
    return {k: parent[k] for k in ENV_ALLOWLIST if k in parent}


def python_env(parent: Optional[dict] = None, path: Optional[Sequence[str]] = None, *,
               windows: Optional[bool] = None) -> dict:
    """``minimal_env`` plus the import path a Python child needs to load this package and its dependencies."""
    env = minimal_env(parent, windows=windows)
    path = sys.path if path is None else path
    dirs = [str(PACKAGE_ROOT)]
    for p in path:
        if p and p not in dirs and os.path.isdir(p):
            dirs.append(p)
    env["PYTHONPATH"] = os.pathsep.join(dirs)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _check(args: Sequence[str], env: Optional[dict]) -> dict:
    if not args or not os.path.isabs(str(args[0])):
        raise ValueError(f"helper programs are started by absolute path, not {args[0] if args else '(none)'!r}")
    return minimal_env() if env is None else env


def run(args: Sequence[str], *, env: Optional[dict] = None, **kwargs: Any) -> subprocess.CompletedProcess:
    """``subprocess.run`` with a minimal environment unless ``env`` is given (build it with this module)."""
    return subprocess.run(list(args), env=_check(args, env), **kwargs)


def popen(args: Sequence[str], *, env: Optional[dict] = None, **kwargs: Any) -> subprocess.Popen:
    """``subprocess.Popen`` with a minimal environment unless ``env`` is given (build it with this module)."""
    kwargs.setdefault("close_fds", True)
    return subprocess.Popen(list(args), env=_check(args, env), **kwargs)


def python_popen(module_or_args: Sequence[str], **kwargs: Any) -> subprocess.Popen:
    """Start ``sys.executable`` with ``module_or_args`` and ``python_env()``."""
    return popen([sys.executable, *module_or_args], env=python_env(), **kwargs)
