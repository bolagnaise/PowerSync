"""Regression tests for the FoxESS Cloud missing-SoC guard.

FoxESS Cloud realtime can omit the ``SoC`` variable for a device. Defaulting
to 0% makes the optimizer think the battery is empty and schedule IDLE, so the
coordinator must distinguish a missing reading from a genuine 0%. These tests
cover ``FoxESSCloudEnergyCoordinator._soc_from_values`` in isolation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable


COORDINATOR_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "coordinator.py"
)


def _load_soc_from_values() -> Callable[[dict[str, Any]], float | None]:
    """Extract the static ``_soc_from_values`` helper without importing HA."""
    source = COORDINATOR_PATH.read_text()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "FoxESSCloudEnergyCoordinator":
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and member.name == "_soc_from_values":
                    member.decorator_list = []  # strip @staticmethod
                    module = ast.Module(body=[member], type_ignores=[])
                    ast.fix_missing_locations(module)
                    namespace: dict[str, Any] = {}
                    exec(compile(module, str(COORDINATOR_PATH), "exec"), namespace)
                    return namespace["_soc_from_values"]
    raise AssertionError("FoxESSCloudEnergyCoordinator._soc_from_values not found")


_soc_from_values = _load_soc_from_values()


def test_present_soc_is_returned():
    assert _soc_from_values({"SoC": 57}) == 57.0
    assert _soc_from_values({"SoC": 42.5}) == 42.5


def test_lowercase_soc_key_is_accepted():
    assert _soc_from_values({"soc": 30}) == 30.0


def test_genuine_zero_is_preserved():
    # A real 0% reading must NOT be treated as "missing".
    assert _soc_from_values({"SoC": 0}) == 0.0


def test_missing_soc_returns_none():
    # No SoC variable in the realtime response → unknown, not 0%.
    assert _soc_from_values({"pvPower": 1200, "loadsPower": 800}) is None


def test_null_soc_returns_none():
    assert _soc_from_values({"SoC": None}) is None


def test_unparseable_soc_returns_none():
    assert _soc_from_values({"SoC": "n/a"}) is None


def test_string_number_is_parsed():
    assert _soc_from_values({"SoC": "65"}) == 65.0
