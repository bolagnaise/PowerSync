"""Tests for EV live-status unit normalization."""

from __future__ import annotations

import sys
import types
import ast
import asyncio
import pytest
from types import SimpleNamespace
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

from power_sync.automations.live_status import coordinator_data_to_ev_live_status  # noqa: E402


def test_coordinator_data_to_ev_live_status_converts_kw_to_watts():
    live_status = coordinator_data_to_ev_live_status({
        "battery_level": 72,
        "grid_power": 1.25,
        "solar_power": 5.5,
        "battery_power": -2.0,
        "load_power": 3.75,
        "ev_power": 7.1,
        "is_curtailed": True,
    })

    assert live_status == {
        "battery_soc": 72,
        "grid_power": 1250.0,
        "solar_power": 5500.0,
        "battery_power": -2000.0,
        "load_power": 3750.0,
        "site_load_power": 3750.0,
        "ev_power": 7100.0,
        # Absent from the payload: this coordinator's telemetry is one vintage.
        "ev_power_at_site_sample": None,
        "home_load_basis": "includes_ev",
        "is_curtailed": True,
        "native_zero_export": False,
        "ac_production_curtailed": False,
        "site_observed_at": None,
    }


def test_ev_power_at_site_sample_is_passed_through_when_published():
    """Tesla publishes the EV draw as of the cached site aggregate (Discord #284)."""
    live_status = coordinator_data_to_ev_live_status({
        "ev_power": 7.7,
        "ev_power_at_site_sample": 5.09,
    })

    assert live_status["ev_power"] == 7700.0
    assert live_status["ev_power_at_site_sample"] == 5090.0


@pytest.mark.parametrize("limit,enabled,expected", [
    (0, True, True), (50, True, True), (51, True, False),
    (50, False, False), (None, True, False),
    (float("nan"), True, False), (True, True, False),
])
def test_native_zero_export_uses_readback_including_sungrow_safe_floor(limit, enabled, expected):
    status = coordinator_data_to_ev_live_status({
        "export_limit_enabled": enabled, "export_limit_w": limit,
    })
    assert status["native_zero_export"] is expected
    assert status["is_curtailed"] is expected


def test_sigenergy_zero_export_readback_is_separate_from_external_ac_limit():
    status = coordinator_data_to_ev_live_status({"export_limit_kw": 0.0})
    assert status["native_zero_export"] is True
    assert status["ac_production_curtailed"] is False


def test_normalized_home_load_keeps_gross_site_load_for_ev_headroom():
    live_status = coordinator_data_to_ev_live_status({
        "load_power": 4.67,
        "site_load_power": 5.67,
        "home_load_basis": "excludes_ev",
        "ev_power": 1.0,
    })

    assert live_status["load_power"] == 4670.0
    assert live_status["site_load_power"] == 5670.0
    assert live_status["home_load_basis"] == "excludes_ev"


def test_coordinator_data_to_ev_live_status_handles_missing_values():
    live_status = coordinator_data_to_ev_live_status({"battery_level": None})

    assert live_status["battery_soc"] is None
    assert live_status["grid_power"] == 0.0
    assert live_status["solar_power"] == 0.0
    assert live_status["battery_power"] == 0.0
    assert live_status["load_power"] is None
    assert live_status["site_load_power"] is None
    assert live_status["ev_power"] == 0.0
    assert live_status["is_curtailed"] is False


def test_unavailable_normalized_home_load_is_not_serialized_as_zero():
    live_status = coordinator_data_to_ev_live_status(
        {
            "solar_power": 15.45,
            "grid_power": 13.32,
            "battery_power": -15.0,
            "load_power": None,
            "site_load_power": 13.77,
            "home_load_basis": "excludes_ev",
        }
    )

    assert live_status["load_power"] is None
    assert live_status["site_load_power"] == 13_770.0


def test_ev_live_status_uses_combined_sungrow_site_solar():
    live_status = coordinator_data_to_ev_live_status(
        {
            "solar_power": 7.669,
            "battery_inverter_solar_power": 4.55,
            "ac_inverter_solar_power": 3.119,
        }
    )

    assert live_status["solar_power"] == 7669.0


def test_normal_ac_inverter_does_not_hide_native_zero_export_from_ev_controller():
    """Separate AC-inverter state must not erase native hybrid curtailment."""
    source = (ROOT / "automations/actions.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get_tesla_live_status")

    class StripImports(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            return ast.copy_location(ast.Pass(), node)

    coordinator = SimpleNamespace(data={
        "battery_level": 100, "is_curtailed": True,
        "grid_power": 0, "solar_power": 1.2, "battery_power": 0,
    })
    hass = SimpleNamespace(data={"power_sync": {"entry-id": {
        "sigenergy_coordinator": coordinator, "inverter_last_state": "normal",
    }}})
    namespace = {
        "HomeAssistant": object, "ConfigEntry": object,
        "Optional": __import__("typing").Optional, "Dict": dict,
        "Any": object, "DOMAIN": "power_sync",
        "EV_LIVE_STATUS_COORDINATOR_KEYS": ("sigenergy_coordinator",),
        "_coordinator_has_normalized_ev_live_data": lambda _coord: True,
        "coordinator_data_to_ev_live_status": coordinator_data_to_ev_live_status,
    }
    unit = ast.Module(body=[StripImports().visit(node)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(unit), "<ev-live-status>", "exec"), namespace)
    status = asyncio.run(namespace["_get_tesla_live_status"](
        hass, SimpleNamespace(entry_id="entry-id")
    ))
    assert status["is_curtailed"] is True
