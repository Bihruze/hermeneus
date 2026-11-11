"""Words that name the machine in user-facing text: "this Mac" on macOS, "this PC" on Windows.

macOS strings stay byte for byte what they were; only other platforms get the
other word.
"""

from __future__ import annotations

import sys


def this_machine(platform: str = "") -> str:
    return "this Mac" if (platform or sys.platform) == "darwin" else "this PC"


def the_machine(platform: str = "") -> str:
    return "the Mac" if (platform or sys.platform) == "darwin" else "the PC"
