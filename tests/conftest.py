"""Test setup: make ``hermeneus_engine`` importable from the repo root, whatever the checkout is named."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
