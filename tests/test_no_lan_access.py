"""No test, engine, dashboard, script or root module may reach a non-loopback address.

A connect or bind on the Mac's own network address, or on all interfaces, can
raise the macOS Local Network prompt for whoever runs the suite (A2 S7). The
overlay page proves its loopback bind from the bound socket's own address and
its Host check with a forged header on a 127.0.0.1 connection instead.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("tests", "hermeneus_engine", "dashboard", "scripts")
ROOT_MODULES = ("glue.py", "commands.py", "__init__.py")
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


def scanned_files():
    for name in ROOT_MODULES:
        yield ROOT / name
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.suffix in SUFFIXES and "__pycache__" not in path.parts and path.is_file():
                yield path


def test_scan_covers_dashboard_scripts_and_root_modules():
    names = {p.relative_to(ROOT).as_posix() for p in scanned_files()}
    assert {"glue.py", "commands.py", "__init__.py", "dashboard/plugin_api.py"} <= names
    assert any(n.startswith("scripts/") for n in names)


def test_no_non_loopback_address_in_scanned_files():
    hits = []
    for path in scanned_files():
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if any(p.search(line) for p in FORBIDDEN):
                hits.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
    assert hits == []
