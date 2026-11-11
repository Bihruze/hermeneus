"""Two real processes: one owns a session through ``hermes hermeneus run``'s code path, the other stops it."""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

from pipeline_fakes import remove_tmpdir, short_tmpdir
from plugin_loader import load_plugin_package

from hermeneus_engine.runtime import lock_holder

OWNER = r'''
import asyncio, io, sys, argparse
sys.path[:0] = [{root!r}, {tests!r}]
from pipeline_fakes import SOURCE, make_rig
from plugin_loader import load_plugin_package
pkg = load_plugin_package()
rig = make_rig()
plugin = pkg.commands.Plugin(None, owner="cli", settings=lambda: {{}}, data_dir=lambda: __import__("pathlib").Path({data!r}),
                             build=rig.build, list_sources=lambda: ([SOURCE], []))
args = argparse.Namespace(to="tr", source="BlackHole", speak=False, device=None, voice=None, json=False)
code = asyncio.run(pkg.commands.cmd_run(plugin, args))
print("owner exit", code, flush=True)
sys.exit(code)
'''


def test_stop_from_a_second_process_stops_the_run_owner():
    data = short_tmpdir()
    root = Path(__file__).resolve().parents[1]
    owner = subprocess.Popen([sys.executable, "-c", OWNER.format(root=str(root), tests=str(root / "tests"),
                                                                  data=str(data))],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not lock_holder(data) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lock_holder(data)["pid"] == owner.pid
        pkg = load_plugin_package()
        stopper = pkg.commands.Plugin(None, owner="cli", data_dir=lambda: data)
        t0 = time.monotonic()
        status = asyncio.run(stopper.controller.stop())
        assert status["state"] == "idle", status
        assert owner.wait(5) == 0
        assert time.monotonic() - t0 < 3.0
        assert lock_holder(data) is None
        assert "owner exit 0" in owner.stdout.read()
    finally:
        owner.kill()
        remove_tmpdir(data)
