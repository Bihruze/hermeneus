"""Test setup: make ``hermeneus_engine`` importable from the repo root, whatever the checkout is named."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


import pytest


@pytest.fixture(autouse=True)
def _no_real_overlay_window(monkeypatch):
    """The ``overlay`` setting defaults to on, so a test session would open the real overlay window.

    Every OverlayService made without its own ``show_fn`` gets this stand-in instead; tests of
    ``control.show`` itself pass their own ``popen`` and are not affected.
    """
    from hermeneus_engine.overlay import control, service

    shown = []

    def fake_show(data_dir, settings, *, session_running):
        if not session_running:
            control.show(data_dir, settings, session_running=False)  # raises before it starts anything
        shown.append(data_dir)
        return {"visible": True, "pid": None, "message": "the overlay is open"}

    defaults = dict(service.OverlayService.__init__.__kwdefaults__)
    monkeypatch.setattr(service.OverlayService.__init__, "__kwdefaults__", {**defaults, "show_fn": fake_show})
    return shown
