# book-keeping-ai/tests/conftest.py

"""
conftest.py — path setup for the test suite.

The two services (demand_forecast, entity_detection) use flat imports
internally (e.g. ``from utils import ...``) because they run as standalone
apps inside Docker.

We add both service directories to sys.path so flat internal imports
resolve correctly when the service modules are loaded via importlib.
"""

import sys
from pathlib import Path

_root = Path(__file__).parent.parent

for _p in [
    str(_root),
    str(_root / "demand_forecast"),
    str(_root / "entity_detection"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)