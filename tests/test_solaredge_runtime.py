"""Regression coverage for SolarEdge runtime startup routing."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _async_setup_entry_source() -> str:
    source = INIT_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_setup_entry":
            segment = ast.get_source_segment(source, node)
            assert segment is not None
            return segment
    raise AssertionError("async_setup_entry not found")


def test_solaredge_runtime_bypasses_tesla_credentials():
    setup_source = _async_setup_entry_source()
    pre_tesla_setup = setup_source[
        : setup_source.index(
            "tesla_api_token, tesla_api_provider = get_tesla_api_token"
        )
    ]

    assert "active_battery_system = _active_battery_system(entry, hass)" in pre_tesla_setup
    assert "is_solaredge = active_battery_system == BATTERY_SYSTEM_SOLAREDGE" in pre_tesla_setup
    assert "elif is_solaredge:" in pre_tesla_setup
    assert (
        "Running in SolarEdge mode - Tesla credentials not required"
        in pre_tesla_setup
    )


def test_solaredge_runtime_wires_energy_coordinator():
    setup_source = _async_setup_entry_source()

    assert "solaredge_coordinator = None" in setup_source
    assert "SolarEdgeEnergyCoordinator(" in setup_source
    assert '"solaredge_coordinator": solaredge_coordinator' in setup_source
    assert "elif is_solaredge:" in setup_source
    assert 'battery_system = "solaredge"' in setup_source
    assert "energy_coordinator = solaredge_coordinator" in setup_source
