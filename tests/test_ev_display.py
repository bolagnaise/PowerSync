"""Regression tests for the canonical EV display coordinator."""

from __future__ import annotations

import asyncio
import ast
import importlib.util
from pathlib import Path
import sys

_SPEC = importlib.util.spec_from_file_location(
    "power_sync_ev_display",
    Path(__file__).parents[1] / "custom_components/power_sync/ev_display.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

from power_sync_ev_display import (  # noqa: E402
    EVDisplayCoordinator,
    active_display_loadpoint,
    display_snapshot_to_sensor_data,
    display_snapshot_to_widgets,
)


def _two_vehicle_snapshot() -> dict:
    return {
        "success": True,
        "site": {
            "ev_power_kw": 7.67,
            "surplus_kw": 0.0,
            "observation_quality": "complete",
        },
        "loadpoints": [
            {
                "loadpoint_id": "tessy-id",
                "vehicle_id": "tessy-vin",
                "vehicle_name": "TESSY",
                "connected": True,
                "actual_charging": False,
                "status": "connected_idle",
                "current_power_kw": 0.0,
                "soc": 80,
                "source": "idle",
            },
            {
                "loadpoint_id": "w3-id",
                "vehicle_id": "w3-vin",
                "vehicle_name": "W3RT1E",
                "site_presence": "home",
                "connected": True,
                "actual_charging": True,
                "status": "charging",
                "current_power_kw": 7.67,
                "soc": 78,
                "source": "grid",
            },
        ],
        "modes": {},
    }


def test_sensor_and_widgets_project_the_same_active_vehicle() -> None:
    snapshot = _two_vehicle_snapshot()

    active = active_display_loadpoint(snapshot)
    sensor = display_snapshot_to_sensor_data(snapshot)
    widgets = display_snapshot_to_widgets(snapshot)
    charging_widget = next(item for item in widgets if item["is_charging"])

    assert active["vehicle_name"] == "W3RT1E"
    assert sensor["vehicle_name"] == charging_widget["vehicle_name"] == "W3RT1E"
    assert sensor["vehicle_id"] == charging_widget["vehicle_id"] == "w3-vin"
    assert sensor["site_presence"] == "home"
    assert sensor["ev_power_kw"] == charging_widget["current_power_kw"] == 7.67
    assert next(item for item in widgets if item["vehicle_name"] == "TESSY") == {
        "vehicle_name": "TESSY",
        "vehicle_id": "tessy-vin",
        "charger_type": None,
        "is_charging": False,
        "is_connected": True,
        "current_soc": 80,
        "target_soc": 80,
        "current_power_kw": 0.0,
        "source": "idle",
        "eta_minutes": None,
        "surplus_kw": 0.0,
    }
    assert sensor["observation_quality"] == "complete"


def test_auxiliary_power_does_not_override_canonical_idle_state() -> None:
    snapshot = {
        "site": {
            "ev_power_kw": 0.0,
            "observed_ev_load_kw": 0.58,
            "observation_quality": "complete",
        },
        "loadpoints": [
            {
                "loadpoint_id": "w3-id",
                "vehicle_id": "w3-vin",
                "vehicle_name": "W3",
                "connected": True,
                "actual_charging": False,
                "status": "connected_idle",
                "current_power_kw": 0.0,
            }
        ],
    }

    sensor = display_snapshot_to_sensor_data(snapshot)
    widgets = display_snapshot_to_widgets(snapshot)

    assert sensor["ev_power_kw"] == 0.0
    assert sensor["is_charging"] is False
    assert widgets[0]["current_power_kw"] == 0.0
    assert widgets[0]["is_charging"] is False


def test_display_coordinator_shares_one_refresh_with_all_consumers() -> None:
    calls = 0

    async def load_snapshot() -> dict:
        nonlocal calls
        calls += 1
        return _two_vehicle_snapshot()

    async def exercise() -> None:
        coordinator = EVDisplayCoordinator(load_snapshot)
        published = []
        coordinator.async_add_listener(published.append)

        first, second = await asyncio.gather(
            coordinator.async_refresh(),
            coordinator.async_refresh(),
        )

        assert calls == 1
        assert first == second == _two_vehicle_snapshot()
        assert published == [_two_vehicle_snapshot()]

    asyncio.run(exercise())


def test_display_coordinator_coalesces_telemetry_refresh_requests() -> None:
    calls = 0

    async def load_snapshot() -> dict:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return _two_vehicle_snapshot()

    async def exercise() -> None:
        coordinator = EVDisplayCoordinator(load_snapshot)
        first, second = await asyncio.gather(
            coordinator.async_request_refresh(),
            coordinator.async_request_refresh(),
        )
        assert calls == 1
        assert first == second == _two_vehicle_snapshot()

    asyncio.run(exercise())


def test_display_coordinator_coalesces_normal_and_forced_refresh() -> None:
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_snapshot() -> dict:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _two_vehicle_snapshot()

    async def exercise() -> None:
        coordinator = EVDisplayCoordinator(load_snapshot)
        normal = asyncio.create_task(coordinator.async_refresh())
        await started.wait()
        forced = asyncio.create_task(coordinator.async_request_refresh())
        await asyncio.sleep(0)
        release.set()

        first, second = await asyncio.gather(normal, forced)
        assert calls == 1
        assert first == second == _two_vehicle_snapshot()

    asyncio.run(exercise())


def test_active_dashboard_paths_use_the_shared_display_coordinator() -> None:
    root = Path(__file__).parents[1]
    init_tree = ast.parse(
        (root / "custom_components/power_sync/__init__.py").read_text()
    )
    sensor_tree = ast.parse(
        (root / "custom_components/power_sync/sensor.py").read_text()
    )

    widget_get = next(
        child
        for node in init_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EVWidgetDataView"
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef) and child.name == "get"
    )
    loadpoint_get = next(
        child
        for node in init_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "EVLoadpointStatusView"
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef) and child.name == "get"
    )
    sensor_update = next(
        child
        for node in ast.walk(sensor_tree)
        if isinstance(node, ast.ClassDef) and node.name == "EVStatusSensor"
        for child in node.body
        if isinstance(child, ast.AsyncFunctionDef) and child.name == "_async_update_ev"
    )
    display_factory = next(
        node
        for node in init_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_get_ev_display_coordinator"
    )

    widget_source = ast.unparse(widget_get)
    loadpoint_source = ast.unparse(loadpoint_get)
    sensor_source = ast.unparse(sensor_update)
    display_factory_source = ast.unparse(display_factory)
    assert "_get_ev_display_coordinator" in widget_source
    assert "_get_ev_vehicles_status" not in widget_source
    assert "_get_ev_display_coordinator" in loadpoint_source
    assert ".async_refresh()" in loadpoint_source
    assert "_get_ev_display_coordinator" in sensor_source
    assert "observed_vehicle_sink=observed_vehicles" in display_factory_source
    assert "vehicles = _get_ev_vehicles_status" not in display_factory_source
    assert "site['observed_ev_load_kw'] = observed_load.power_kw" in display_factory_source
    assert "site['ev_power_kw'] = observed_load.power_kw" not in display_factory_source
    assert "normalized_site = status_view._site_snapshot()" in display_factory_source
    assert "site.update(normalized_site)" in display_factory_source


def test_ha_energy_flow_prefers_canonical_sensor_vehicle_attributes() -> None:
    source = (
        Path(__file__).parents[1]
        / "custom_components/power_sync/frontend/power-sync-energy-flow.js"
    ).read_text()

    assert "powerState?.attributes?.vehicle_name" in source
    assert "canonicalConnected ?? isTruthyPresenceState(presenceState)" in source
    assert "canonicalCharging ?? switchState?.state === 'on'" in source
    assert "vehicle.canonicalLabel || vehicle.customLabel" in source
