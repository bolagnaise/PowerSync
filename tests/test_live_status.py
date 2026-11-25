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
        "battery_soc_known": True,
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
    from datetime import datetime, timezone
    from power_sync.automations.live_status import site_live_status

    source = (ROOT / "automations/actions.py").read_text()
    node = next(n for n in ast.parse(source).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "_get_tesla_live_status")
    assert "site_live_status(entry_data" in ast.get_source_segment(source, node)

    coordinator = SimpleNamespace(last_update_success=True, data={
        "battery_level": 100, "is_curtailed": True,
        "grid_power": 0, "solar_power": 1.2, "battery_power": 0,
    })
    status, configured = site_live_status(
        {"sigenergy_coordinator": coordinator, "inverter_last_state": "normal"},
        datetime.now(timezone.utc),
    )
    assert configured is True
    assert status["is_curtailed"] is True


# --- Shared site live-status builder ------------------------------------------

def _site(**overrides):
    from datetime import datetime, timezone

    coordinator = SimpleNamespace(
        last_update_success=True,
        last_update_success_time=datetime.now(timezone.utc),
        update_interval=None,
        data={"grid_power": -1.0, "solar_power": 3.0, "battery_power": 0.0, "load_power": 2.0},
    )
    for key, value in overrides.items():
        setattr(coordinator, key, value)
    return coordinator


def test_site_live_status_skips_an_unusable_coordinator_for_a_usable_one():
    from datetime import datetime, timedelta, timezone
    from power_sync.automations.live_status import site_live_status

    now = datetime.now(timezone.utc)
    stale = _site(last_update_success_time=now - timedelta(minutes=10))
    fresh = _site()
    fresh.data = {**fresh.data, "grid_power": -2.5}
    status, configured = site_live_status(
        {"tesla_coordinator": stale, "sungrow_coordinator": fresh}, now,
    )
    assert configured is True
    assert status["grid_power"] == -2500


@pytest.mark.parametrize("problem", [
    {"last_update_success": False},
    {"data": {"telemetry_ready": False, "grid_power": 0, "solar_power": 0, "battery_power": 0}},
    {"data": {"grid_power": None, "solar_power": 0, "battery_power": 0}},
    {"stale": True},
])
def test_site_live_status_fails_closed_when_no_coordinator_is_usable(problem):
    from datetime import datetime, timedelta, timezone
    from power_sync.automations.live_status import site_live_status

    now = datetime.now(timezone.utc)
    if problem.pop("stale", False):
        problem = {"last_update_success_time": now - timedelta(minutes=10)}
    assert site_live_status({"tesla_coordinator": _site(**problem)}, now) == (None, True)
    assert site_live_status({}, now) == (None, False)


def test_site_live_status_rejects_fronius_values_retained_past_their_source_time():
    from datetime import datetime, timedelta, timezone
    from power_sync.automations.live_status import site_live_status

    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=1)
    fronius = _site()
    fronius.data = {**fronius.data, "telemetry_ready": True, "telemetry_observed_at": {
        "load_power": now, "grid_power": old, "battery_power": now,
    }}
    assert site_live_status({"fronius_reserva_coordinator": fronius}, now) == (None, True)
    fronius.data["telemetry_observed_at"]["grid_power"] = now
    assert site_live_status({"fronius_reserva_coordinator": fronius}, now)[0] is not None


def test_status_views_read_the_site_through_the_shared_builder():
    """The status and price views must not pick coordinators or skip freshness themselves."""
    source = (ROOT / "__init__.py").read_text()
    for view, nxt in (("class SolarSurplusStatusView", "\nclass "),
                      ("class PriceRecommendationView", "\nclass ")):
        start = source.index(view)
        body = source[start:source.index(nxt, start + 1)]
        assert "_surplus_view_site(" in body
        assert 'entry_data.get("tesla_coordinator")' not in body


@pytest.mark.parametrize(("data", "expected"), [
    ({"battery_level": 55}, 55.0),
    ({"battery_level": 0}, 0.0),
    ({}, None),
    ({"battery_level": None}, None),
    ({"battery_level": "unavailable"}, None),
    ({"battery_level": 140}, None),
])
def test_ev_home_battery_soc_never_reads_a_missing_level_as_zero(data, expected):
    from power_sync.automations.live_status import ev_home_battery_soc

    live_status = coordinator_data_to_ev_live_status(data)
    assert ev_home_battery_soc(live_status) == expected
    # Curtailment keeps its legacy numeric default.
    assert live_status["battery_soc"] == data.get("battery_level", 0)


def test_ev_home_battery_soc_accepts_a_tesla_api_status_without_the_flag():
    from power_sync.automations.live_status import ev_home_battery_soc

    assert ev_home_battery_soc({"battery_soc": 64}) == 64.0
    assert ev_home_battery_soc({"battery_soc": None}) is None
