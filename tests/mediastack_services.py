"""Load the simulator's service list without importing it as a script."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "mediastack" / "simulate.py"
_spec = importlib.util.spec_from_file_location("mediastack_simulate", _PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["mediastack_simulate"] = _module
_spec.loader.exec_module(_module)

SERVICES = _module.SERVICES
SERVICE_NAMES = {s.name for s in SERVICES}
failure_rate = _module.failure_rate
load_injections = _module.load_injections
