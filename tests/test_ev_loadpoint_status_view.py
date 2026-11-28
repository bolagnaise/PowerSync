"""Regression tests for normalized EV loadpoint endpoint wiring."""

from __future__ import annotations

import ast
import math
from pathlib import Path
from types import SimpleNamespace

import importlib.util

import pytest


def _zaptec_power_draw():
    spec = importlib.util.spec_from_file_location(
        "zaptec_state_test", ROOT / "custom_components/power_sync/zaptec_state.py",
    )
    state = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(state)
    return state.zaptec_power_draw


def _zaptec_display_projection(byd_vehicles, other_observations=(), stored_data=None,
                               mode="charging", power=None):
    """Execute the shipped display enrichment without HA or device commands."""
    method = _get_method()
    body = method.body[1].body
    # Use the real observation-to-display block before canonical normalization.
    start = next(i for i, node in enumerate(body) if isinstance(node, ast.If)
                 and ast.unparse(node.test) == 'zaptec_cached')
    end = next(i for i, node in enumerate(body) if isinstance(node, ast.If)
               and ast.unparse(node.test) == 'observed_vehicle_sink is not None')
    blocks = [node for node in body[start:end]
              if isinstance(node, ast.If) and ast.unparse(node.test) == 'zaptec_cached']
    spec = importlib.util.spec_from_file_location(
        'zaptec_display_test', ROOT / 'custom_components/power_sync/ev_display.py')
    display = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(display)
    spec = importlib.util.spec_from_file_location(
        'zaptec_profiles_test', ROOT / 'custom_components/power_sync/automations/ev_loadpoint_profiles.py')
    profiles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profiles)

    stored_data = stored_data or {}
    namespace = {
        'zaptec_cached': {'charger_operation_mode': mode, 'total_charge_power_w': power if power is not None else (10700 if mode == 'charging' else 0)},
        'zaptec_power_draw': _zaptec_power_draw(),
        'observed_vehicles': list(other_observations),
        'stored_data': stored_data,
        'vehicle_configs': stored_data.get('vehicle_charging_configs', []),
        'self': SimpleNamespace(_hass=object()),
        'EVVehiclesView': lambda hass: SimpleNamespace(_get_byd_vehicles=lambda: byd_vehicles),
        'resolve_zaptec_display_soc': getattr(display, 'resolve_zaptec_display_soc', None),
        'effective_profile': profiles.effective_profile,
        'profiles_for_loadpoint': profiles.profiles_for_loadpoint,
    }
    module = ast.Module(body=blocks, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(INIT_PATH), 'exec'), namespace)
    return next(row for row in namespace['observed_vehicles'] if row.get('charger_type') == 'zaptec')


def test_zaptec_mode_three_at_zero_watts_is_connected_and_waiting():
    row = _zaptec_display_projection([], mode="charging", power=0)
    assert row["is_connected"] is True
    assert row["is_charging"] is False
    assert row["blocking_reason"] == "connected_waiting"


def test_zaptec_energy_flow_receives_available_byd_percentage():
    row = _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': 70, 'is_plugged_in': True},
    ])
    assert row.get('ev_soc') == 70
    assert row['charger_id'] == 'zaptec_standalone'
    assert row['ev_power_kw'] == 10.7


@pytest.mark.parametrize('mode', ['connected_waiting', 'connected_finished', 'disconnected'])
def test_zaptec_byd_percentage_follows_physical_connection(mode):
    row = _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': 0, 'is_plugged_in': True},
    ], mode=mode)
    assert row.get('ev_soc') == (None if mode == 'disconnected' else 0)


@pytest.mark.parametrize('soc', [None, 'unknown', 'unavailable', -1, 101, True, float('nan'), float('inf')])
def test_zaptec_byd_display_keeps_invalid_soc_unknown(soc):
    assert _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': soc, 'is_plugged_in': True},
    ]).get('ev_soc') is None


def test_zaptec_connection_can_supply_presence_when_byd_plug_sensor_is_off():
    assert _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': 70, 'is_plugged_in': False},
    ]).get('ev_soc') == 70


@pytest.mark.parametrize('other_type', ['tesla', 'generic', 'ocpp', 'sigenergy'])
def test_zaptec_byd_display_does_not_borrow_soc_across_loadpoints(other_type):
    assert _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': 70, 'is_plugged_in': True},
    ], [{'vehicle_id': 'other_car', 'charger_type': other_type}]).get('ev_soc') is None


@pytest.mark.parametrize('selected', [None, 'byd_device_a', 'byd_device_b', 'byd_missing'])
def test_zaptec_shared_profiles_use_only_the_explicitly_selected_byd(selected):
    vehicles = [
        {'vehicle_id': 'byd_device_a', 'battery_level': 70, 'is_plugged_in': True},
        {'vehicle_id': 'byd_device_b', 'battery_level': 82, 'is_plugged_in': True},
    ]
    stored_data = {
        'vehicle_charging_configs': [
            {'vehicle_id': 'byd_device_a', 'charger_type': 'zaptec'},
            {'vehicle_id': 'byd_device_b', 'charger_type': 'zaptec'},
        ],
        'connected_ev_by_loadpoint': {'zaptec_standalone': selected},
    }
    row = _zaptec_display_projection(vehicles, stored_data=stored_data)
    expected = {'byd_device_a': 70, 'byd_device_b': 82}.get(selected)
    assert row.get('ev_soc') == expected
    assert row['charger_id'] == 'zaptec_standalone'


def test_zaptec_display_does_not_guess_when_second_byd_soc_is_unavailable():
    row = _zaptec_display_projection([
        {'vehicle_id': 'byd_device_a', 'battery_level': 70, 'is_plugged_in': True},
        {'vehicle_id': 'byd_device_b', 'battery_level': None, 'is_plugged_in': False},
    ])
    assert row.get('ev_soc') is None


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
            and node.func.id == "_site_surplus_kw"
        )
    ]

    assert len(calls) == 1
    assert isinstance(calls[0].args[2], ast.Name)
    assert calls[0].args[2].id == "total_ev_power_kw"
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
    # The display snapshot must not feed surplus arithmetic directly.
    assert "_calculate_solar_surplus" not in ast.unparse(method)


def _extract_site_surplus(monkeypatch, calculate):
    import sys
    import types
    from datetime import datetime, timezone

    root = INIT_PATH.parent
    package = types.ModuleType("power_sync")
    package.__path__ = [str(root)]
    automations = types.ModuleType("power_sync.automations")
    automations.__path__ = [str(root / "automations")]
    monkeypatch.setitem(sys.modules, "power_sync", package)
    monkeypatch.setitem(sys.modules, "power_sync.automations", automations)
    monkeypatch.delitem(sys.modules, "power_sync.automations.live_status", raising=False)
    actions = types.ModuleType("power_sync.automations.actions")
    actions._calculate_solar_surplus = calculate
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions)

    tree = ast.parse(INIT_PATH.read_text())
    method = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_site_surplus_kw"
    )
    module = ast.Module(body=[method], type_ignores=[])
    ast.fix_missing_locations(module)
    now = datetime(2026, 11, 28, 8, 26, tzinfo=timezone.utc)
    namespace = {
        "DOMAIN": "power_sync",
        "__name__": "power_sync._status_view",
        "__package__": "power_sync",
        "dt_util": SimpleNamespace(utcnow=lambda: now),
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)
    return namespace["_site_surplus_kw"], now


def _surplus_site(coordinators):
    return (
        SimpleNamespace(data={"power_sync": {"entry-1": coordinators}}),
        SimpleNamespace(entry_id="entry-1"),
    )


def test_stale_site_telemetry_reports_no_spare_solar_at_night(monkeypatch):
    """Live 2026-11-28 18:26: 5.4 kW "solar to spare" while the battery fed the car.

    The display snapshot took a coordinator whose last refresh had failed and
    paired its retained battery sample with the live 7.6 kW EV draw.
    """
    from datetime import timedelta

    seen = []

    def calculate(live_status, ev_power_kw, config):
        seen.append(live_status)
        return 5.4

    site_surplus, now = _extract_site_surplus(monkeypatch, calculate)
    retained = SimpleNamespace(
        last_update_success=False,
        last_update_success_time=now - timedelta(minutes=20),
        update_interval=timedelta(seconds=30),
        data={"solar_power": 0.0, "grid_power": 0.0, "battery_power": 2.2},
    )
    hass, entry = _surplus_site({"tesla_coordinator": retained})

    assert site_surplus(hass, entry, 7.6, {}) == 0.0
    assert seen == []


def test_spare_solar_uses_the_fresh_site_sample(monkeypatch):
    from datetime import timedelta

    seen = []

    def calculate(live_status, ev_power_kw, config):
        seen.append((live_status, ev_power_kw))
        return 0.0

    site_surplus, now = _extract_site_surplus(monkeypatch, calculate)
    fresh = SimpleNamespace(
        last_update_success=True,
        last_update_success_time=now - timedelta(seconds=10),
        update_interval=timedelta(seconds=30),
        data={
            "solar_power": 0.0,
            "grid_power": 0.0,
            "battery_power": 8.87,
            "ev_power": 7.6,
            "ev_power_at_site_sample": 7.6,
            "is_curtailed": True,
        },
    )
    hass, entry = _surplus_site({"tesla_coordinator": fresh})

    assert site_surplus(hass, entry, 7.6, {}) == 0.0
    live_status, ev_power_kw = seen[0]
    assert live_status["battery_power"] == pytest.approx(8870)
    # The drift sample reaches the calculation, as it does for the controllers.
    assert live_status["ev_power_at_site_sample"] == pytest.approx(7600)
    assert live_status["is_curtailed"] is True
    assert ev_power_kw == 7.6


def test_loadpoint_status_preserves_curtailed_site_state_for_surplus_calculation():
    snapshot = _get_site_snapshot_method()
    snapshot_source = ast.unparse(snapshot)

    assert "data.get('is_curtailed', False) is True" in snapshot_source
    assert "'is_curtailed': is_curtailed" in snapshot_source

    # Surplus reads curtailment from the shared fresh live status (see
    # test_spare_solar_uses_the_fresh_site_sample), not this display dict.

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


def test_loadpoint_site_snapshot_keeps_stale_core_telemetry_unavailable():
    """Ticket-39: stale coordinator data is not a measured zero-valued site."""
    site_snapshot = _extract_site_snapshot()
    coordinator = SimpleNamespace(
        data={
            "solar_power": 7.27,
            "grid_power": -0.1,
            "battery_power": 1.2,
            "load_power": 1.5,
            "battery_level": 53,
            "telemetry_ready": False,
        },
    )
    view = SimpleNamespace(
        _hass=SimpleNamespace(
            data={"power_sync": {"entry-1": {"sungrow_coordinator": coordinator}}},
        ),
        _config_entry=SimpleNamespace(entry_id="entry-1"),
    )

    snapshot = site_snapshot(view)
    assert snapshot["solar_power_kw"] is None
    assert snapshot["grid_power_kw"] is None
    assert snapshot["battery_power_kw"] is None
    assert snapshot["load_power_kw"] is None
    assert snapshot["battery_soc"] is None

    coordinator.data["telemetry_ready"] = True
    coordinator.data.update(
        solar_power=0.0,
        grid_power=0.0,
        battery_power=0.0,
        load_power=0.0,
        battery_level=0.0,
    )
    assert site_snapshot(view) == {
        "battery_soc": 0.0,
        "solar_power_kw": 0.0,
        "grid_power_kw": 0.0,
        "battery_power_kw": 0.0,
        "load_power_kw": 0.0,
        "is_curtailed": False,
    }


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


def test_zaptec_finished_mode_is_kept_as_a_connected_loadpoint():
    method = _get_method()
    body = method.body[1].body
    index = next(
        index
        for index, node in enumerate(body)
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "zaptec_cached"
                for target in node.targets
            )
        )
    )
    module = ast.Module(body=body[index : index + 2], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "entry_data": {
            "zaptec_cached_state": {
                "charger_operation_mode": "connected_finished",
                "total_charge_power_w": 0,
            }
        },
        "observed_vehicles": [],
        "zaptec_power_draw": _zaptec_power_draw(),
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)

    assert namespace["observed_vehicles"] == [{
        "charger_id": "zaptec_standalone",
        "vehicle_name": "Zaptec Charger",
        "charger_type": "zaptec",
        "ev_power_kw": 0.0,
        "is_connected": True,
        "is_charging": False,
        "blocking_reason": "connected_finished",
    }]


def test_ble_loadpoint_observations_use_current_entity_compatibility_helpers():
    tree = ast.parse(INIT_PATH.read_text())
    source = ast.unparse(tree)

    assert "get_tesla_ble_battery_state(hass, prefix)" in source
    assert "get_tesla_ble_charge_current_state(hass, prefix)" in source
    assert "get_tesla_ble_charging_state(hass, prefix)" in source
    assert "get_tesla_ble_charge_power_state(hass, prefix)" in source
    assert "get_tesla_ble_plug_state(hass, prefix)" in source


def test_ble_unavailable_power_survives_the_real_status_view_projection():
    """Ticket-36: the endpoint must not turn unavailable BLE power into idle."""
    source = ast.unparse(_get_method())

    assert "'power_available': vehicle.get('power_available', True)" in source
    assert "'current_amps': vehicle.get('current_amps')" in source


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
    assert "'_user_initiated': True" in source
    # The old raw path bypassed both the controller and the phase clamp.
    assert "'action_type': 'start_ev_charging'" not in source
    assert "'skip_ownership': True" not in source
    # The expiry stop must tear the controller down rather than only sending a
    # physical stop and releasing the lease by hand.
    stop_source = ast.unparse(next(
        node for node in ast.walk(ast.parse(INIT_PATH.read_text()))
        if isinstance(node, ast.FunctionDef) and node.name == "_arm_boost_stop"
    ))
    transaction = ast.unparse(_get_async_class_method("ChargingBoostView", "_start_finite_boost"))
    assert "self._start_finite_boost(" in source
    assert "self._arm_boost_stop(" in transaction
    assert "'action_type': 'stop_ev_charging_dynamic'" in stop_source
    assert "'stop_charging': True" in stop_source
    assert "release_ev_ownership" not in source


def test_boost_does_not_re_raise_current_past_the_phase_clamp():
    source = ast.unparse(_get_async_class_method("ChargingBoostView", "post"))

    # start_ev_charging_dynamic already commands the phase-safe current through
    # _set_vehicle_amps. A follow-up raw set_ev_charging_amps would push the
    # charger straight back to 32 A and undo the shared budget decision.
    assert "'action_type': 'set_ev_charging_amps'" not in source
    transaction = ast.unparse(_get_async_class_method("ChargingBoostView", "_start_finite_boost"))
    assert "applied_amps" in transaction
    assert "'action_type': 'set_ev_charging_amps'" not in transaction
