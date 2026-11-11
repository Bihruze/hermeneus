"""Every helper process goes through hermeneus_engine.subproc: absolute program paths, minimal environment."""
import re
from pathlib import Path

import pytest

from hermeneus_engine import subproc

PKG = Path(subproc.__file__).resolve().parent
SPAWN = re.compile(r"\bsubprocess\.(run|Popen|call|check_call|check_output|getoutput|getstatusoutput)\b"
                   r"|\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)\s*\("
                   r"|\bcreate_subprocess_(exec|shell)\b")


def test_no_module_spawns_processes_except_subproc():
    offenders = []
    for path in sorted(PKG.rglob("*.py")):
        if path == Path(subproc.__file__).resolve():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if SPAWN.search(code):
                offenders.append(f"{path.relative_to(PKG.parent)}:{n}: {line.strip()}")
    assert offenders == []


def test_minimal_env_drops_secrets_and_keeps_the_allowlist():
    parent = {"PATH": "/usr/bin", "HOME": "/Users/x", "LANG": "en_US.UTF-8", "ELEVENLABS_API_KEY": "k",
              "GITHUB_TOKEN": "t", "AWS_SECRET_ACCESS_KEY": "s", "PYTHONHOME": "/x"}
    env = subproc.minimal_env(parent, windows=False)
    assert env == {"PATH": "/usr/bin", "HOME": "/Users/x", "LANG": "en_US.UTF-8"}
    win = subproc.minimal_env({"SystemRoot": r"C:\Windows", "Path": "p", "OPENAI_API_KEY": "k"}, windows=True)
    assert win == {"SYSTEMROOT": r"C:\Windows", "PATH": "p"}


def test_programs_are_named_by_absolute_path():
    with pytest.raises(ValueError, match="absolute path"):
        subproc.run(["system_profiler", "SPAudioDataType"])
    with pytest.raises(ValueError, match="absolute path"):
        subproc.popen(["ps"])
    assert subproc.SYSTEM_PROFILER == "/usr/sbin/system_profiler" and subproc.PS == "/bin/ps"


def test_run_passes_the_minimal_env(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "sk-test")
    seen = {}
    monkeypatch.setattr(subproc.subprocess, "run", lambda args, **kw: seen.update(kw))
    subproc.run([subproc.PS, "-p", "1"], capture_output=True)
    assert "ELEVENLABS_API_KEY" not in seen["env"]
