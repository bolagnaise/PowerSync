"""Regression tests for normalized EV loadpoint endpoint wiring."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"


def _get_method() -> ast.AsyncFunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EVLoadpointStatusView":
            for child in node.body:
                if (
                    isinstance(child, ast.AsyncFunctionDef)
                    and child.name == "_async_build_response"
                ):
                    return child
    raise AssertionError("EVLoadpointStatusView._async_build_response not found")


def _get_site_snapshot_method() -> ast.FunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "EVLoadpointStatusView":
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name == "_site_snapshot":
                    return child
    raise AssertionError("EVLoadpointStatusView._site_snapshot not found")


def _extract_site_snapshot():
    method = _get_site_snapshot_method()
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"DOMAIN": "power_sync", "math": math}
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace["_site_snapshot"]


def test_loadpoint_site_surplus_uses_normalized_total_ev_power():
    method = _get_method()
    calls = [
        node
        for node in ast.walk(method)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_calculate_solar_surplus"
        )
    ]

    assert len(calls) == 1
    assert len(calls[0].args) >= 2
    assert isinstance(calls[0].args[1], ast.Name)
    assert calls[0].args[1].id == "total_ev_power_kw"
    assert any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "preliminary_loadpoints"
            for target in node.targets
        )
        and node.lineno < calls[0].lineno
        for node in ast.walk(method)
    )


def test_loadpoint_status_preserves_curtailed_site_state_for_surplus_calculation():
    snapshot = _get_site_snapshot_method()
    snapshot_source = ast.unparse(snapshot)

    assert "coordinator.data.get('is_curtailed', False) is True" in snapshot_source
    assert "'is_curtailed': is_curtailed" in snapshot_source

    method_source = ast.unparse(_get_method())
    assert "'is_curtailed': site['is_curtailed']" in method_source

    site_snapshot = _extract_site_snapshot()
    base_data = {
        "solar_power": 7.27,
        "grid_power": -0.1,
        "battery_power": 0.0,
        "load_power": 1.5,
        "battery_level": 100,
    }
    for raw_value, expected in (
        (True, True),
        (False, False),
        (None, False),
        ("false", False),
    ):
        coordinator = SimpleNamespace(
            data={**base_data, "is_curtailed": raw_value},
        )
        view = SimpleNamespace(
            _hass=SimpleNamespace(
                data={
                    "power_sync": {
                        "entry-1": {"tesla_coordinator": coordinator},
                    },
                },
            ),
            _config_entry=SimpleNamespace(entry_id="entry-1"),
        )

        assert site_snapshot(view)["is_curtailed"] is expected


def test_loadpoint_site_snapshot_preserves_unavailable_home_load():
    site_snapshot = _extract_site_snapshot()
    coordinator = SimpleNamespace(
        data={
            "solar_power": 7.27,
            "grid_power": 10.0,
            "battery_power": -7.8,
            "load_power": None,
            "battery_level": 53,
        },
    )
    view = SimpleNamespace(
        _hass=SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {"sungrow_coordinator": coordinator},
                },
            },
        ),
        _config_entry=SimpleNamespace(entry_id="entry-1"),
    )

    assert site_snapshot(view)["load_power_kw"] is None

    coordinator.data["load_power"] = 0.0
    assert site_snapshot(view)["load_power_kw"] == 0.0


def test_hacs_ocpp_discovery_is_enabled_and_claim_filtered():
    method = _get_method()
    source = ast.unparse(method)

    assert "if opts.get(CONF_OCPP_ENABLED) else ()" in source
    assert "claimed_hacs_ocpp_prefixes" in source
    assert "prefix in claimed_prefixes" in source


def test_configured_vehicle_profiles_are_kept_as_idle_loadpoints():
    method = _get_method()
    source = ast.unparse(method)

    assert "stored_data.get('vehicle_charging_configs', [])" in source
    assert "'include_idle': any((vehicle_ids_match(configured_id, vehicle_id)" in source


def test_ble_loadpoint_observations_use_current_entity_compatibility_helpers():
    tree = ast.parse(INIT_PATH.read_text())
    source = ast.unparse(tree)

    assert "get_tesla_ble_battery_state(hass, prefix)" in source
    assert "get_tesla_ble_charge_current_state(hass, prefix)" in source
    assert "get_tesla_ble_charging_state(hass, prefix)" in source
    assert "get_tesla_ble_charge_power_state(hass, prefix)" in source
    assert "get_tesla_ble_plug_state(hass, prefix)" in source


def _get_async_class_method(class_name: str, method_name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == method_name:
                    return child
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_boost_runs_through_the_dynamic_controller_for_the_whole_window():
    source = ast.unparse(_get_async_class_method("ChargingBoostView", "post"))

    # The start must create a managed session, not a raw one-shot command.
    assert "'action_type': 'start_ev_charging_dynamic'" in source
    assert "'owner_mode': 'boost'" in source
    assert "'fixed_charge_amps': 32" in source
    assert "'phase_requested_amps': 32" in source
    assert "'phase_load_management_required': True" in source
    # The old raw path bypassed both the controller and the phase clamp.
    assert "'action_type': 'start_ev_charging'" not in source
    assert "'skip_ownership': True" not in source
    # The expiry stop must tear the controller down rather than only sending a
    # physical stop and releasing the lease by hand.
    assert "'action_type': 'stop_ev_charging_dynamic'" in source
    assert "'stop_charging': True" in source
    assert "release_ev_ownership" not in source


def test_boost_does_not_re_raise_current_past_the_phase_clamp():
    source = ast.unparse(_get_async_class_method("ChargingBoostView", "post"))

    # start_ev_charging_dynamic already commands the phase-safe current through
    # _set_vehicle_amps. A follow-up raw set_ev_charging_amps would push the
    # charger straight back to 32 A and undo the shared budget decision.
    assert "'action_type': 'set_ev_charging_amps'" not in source
    assert "applied_boost_amps" in source
