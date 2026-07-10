"""Regression test: TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS must be a single
shared constant in const.py, not four independently drifting `= 30`
literals across number.py, select.py, sensor.py, and
optimization/battery_controller.py.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_LOCAL_DEFINITION = re.compile(
    r"^(TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS|_LOCAL_STALE_SECONDS)\s*=\s*30\s*$",
    re.MULTILINE,
)


def test_const_defines_shared_tesla_local_control_max_age():
    const_source = (COMPONENT_ROOT / "const.py").read_text()
    assert "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS = 30" in const_source


def _assert_imports_shared_constant(path: Path) -> None:
    source = path.read_text()
    assert not _LOCAL_DEFINITION.search(source), (
        f"{path.name} still defines its own "
        "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS/_LOCAL_STALE_SECONDS literal "
        "instead of importing the shared constant from const.py"
    )
    assert "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS" in source, (
        f"{path.name} does not reference the shared "
        "TESLA_LOCAL_CONTROL_MAX_AGE_SECONDS constant"
    )


def test_number_platform_imports_shared_constant():
    _assert_imports_shared_constant(COMPONENT_ROOT / "number.py")


def test_select_platform_imports_shared_constant():
    _assert_imports_shared_constant(COMPONENT_ROOT / "select.py")


def test_sensor_platform_imports_shared_constant():
    _assert_imports_shared_constant(COMPONENT_ROOT / "sensor.py")


def test_battery_controller_imports_shared_constant():
    _assert_imports_shared_constant(
        COMPONENT_ROOT / "optimization" / "battery_controller.py"
    )
