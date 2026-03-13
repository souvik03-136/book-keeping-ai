"""
conftest.py — path setup for the test suite.

The two services (demand_forecast, entity_detection) use flat imports
internally (e.g. `from utils import ...`) because they run as standalone
apps inside Docker.  Tests import them as packages from the repo root
(e.g. `from demand_forecast.app import app`).

We need BOTH the repo root (so package-style imports work) AND each
service directory (so the flat internal imports resolve) on sys.path.
"""
import sys
from pathlib import Path

root = Path(__file__).parent.parent

for p in [
    str(root),                          # enables: from demand_forecast.app import ...
    str(root / "demand_forecast"),      # enables internal: from utils import ...
    str(root / "entity_detection"),     # enables internal: from utils import ... (if any)
]:
    if p not in sys.path:
        sys.path.insert(0, p)