"""
conftest.py — makes demand_forecast and entity_detection importable
from the tests/ directory without installing them as packages.
"""
import sys
from pathlib import Path

root = Path(__file__).parent.parent
sys.path.insert(0, str(root / "demand_forecast"))
sys.path.insert(0, str(root / "entity_detection"))