"""No test and no engine module may reach a non-loopback address.

A connect or bind on the Mac's own network address, or on all interfaces, can
raise the macOS Local Network prompt for whoever runs the suite (A2 S7). The
overlay page proves its loopback bind from the bound socket's own address and
its Host check with a forged header on a 127.0.0.1 connection instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "hermeneus_engine")
SUFFIXES = {".py", ".js", ".mjs", ".html"}
# Built from parts so this file does not match itself.
FORBIDDEN = [
    re.compile(re.escape("0.0." + "0.0")),
    re.compile(r"gethost" + r"byname\s*\("),
    re.compile(r"gethost" + r"name\s*\("),
    re.compile(r"getfqdn\s*\("),
    re.compile(r"INADDR_" + r"ANY"),
    re.compile(re.escape("192.0." + "2.1")),
]


def test_no_non_loopback_address_in_tests_or_engine():
    hits = []
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.suffix not in SUFFIXES or "__pycache__" in path.parts or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if any(p.search(line) for p in FORBIDDEN):
                    hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert hits == []
