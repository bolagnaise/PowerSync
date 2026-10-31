"""Ticket #451: reloads preserve measured battery inventory and pending saves."""

import ast
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_battery_export_allowed_slots import (
    COMPONENT_ROOT,
    _ProvenanceStore,
    _charging_solve_coordinator,
    _provenance_coordinator,
    _run_to_acquisition_cost,
    opt_module,
)


def _setup_persistence_tail(opt_module):
    """Run the real setup's persistence tail without unrelated provider setup."""
    path = COMPONENT_ROOT / "optimization" / "coordinator.py"
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef)
               and n.name == "OptimizationCoordinator")
    setup = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == "async_setup")
    start = next(i for i, n in enumerate(setup.body)
                 if isinstance(n, ast.Expr) and isinstance(n.value, ast.Await)
                 and isinstance(n.value.value, ast.Call)
                 and isinstance(n.value.value.func, ast.Attribute)
                 and n.value.value.func.attr == "_restore_cost_data")
    setup.body = setup.body[start:]
    namespace = dict(vars(opt_module))
    exec(compile(ast.fix_missing_locations(ast.Module(body=[setup], type_ignores=[])),
                 str(path), "exec"), namespace)
    return namespace["async_setup"]


def _stored_inventory():
    return {"solar_kwh": 3.0, "grid_kwh": 21.03, "grid_cost": 0.0,
            "unknown_kwh": 0.0, "last_soc": 0.89, "last_capacity_wh": 27_000,
            "last_observed_at": "2026-10-31T09:55:00+00:00"}


@pytest.mark.parametrize("data", [
    None, {}, {"solar_power": 1.0}, {"battery_level": None},
    {"battery_level": float("nan")}, {"battery_level": float("inf")},
    {"battery_level": -1.0}, {"battery_level": "unavailable"},
    {"battery_level": True}, {"battery_level": 50.0, "telemetry_ready": False},
])
def test_reload_without_measured_soc_preserves_inventory(opt_module, data):
    """Two reloads followed by 89% must not invent the reported 10.53 kWh gap."""
    store = _ProvenanceStore(_stored_inventory())
    setup_tail = _setup_persistence_tail(opt_module)
    for _ in range(2):
        stored = dict(store.data)
        coordinator = _provenance_coordinator(opt_module, store)
        coordinator._config.battery_capacity_wh = 27_000
        coordinator._restore_cost_data = AsyncMock()
        coordinator._get_energy_data = lambda: data
        asyncio.run(setup_tail(coordinator))
        assert coordinator._battery_provenance_data_to_save() == stored
        assert store.delayed_callback is None
        # The first real measured interval keeps the restored attribution.
        coordinator._record_battery_provenance_interval(
            solar_charge_kwh=0.0, grid_charge_kwh=0.0, grid_charge_cost=0.0,
            discharge_kwh=0.0, current_soc=0.89, capacity_wh=27_000,
            telemetry_gap=True,
        )
        assert coordinator._battery_provenance_unknown_kwh == pytest.approx(0.0)
        assert coordinator._battery_provenance_total_kwh() == pytest.approx(24.03)
        assert coordinator._acquisition_cost_for_run(
            import_prices=[0.285], current_soc=0.89, capacity_wh=27_000,
        ) == pytest.approx(0.0)
        asyncio.run(store.async_save(coordinator._battery_provenance_data_to_save()))
        store.delayed_callback = None


@pytest.mark.parametrize("level", [0.0, 0.6, 50.0, 89.0, 95.0])
def test_startup_measured_soc_still_reconciles_real_drift(opt_module, level):
    coordinator = _provenance_coordinator(opt_module, _ProvenanceStore(_stored_inventory()))
    coordinator._config.battery_capacity_wh = 27_000
    coordinator._restore_cost_data = AsyncMock()
    coordinator._get_energy_data = lambda: {"battery_level": level}
    asyncio.run(_setup_persistence_tail(opt_module)(coordinator))
    assert coordinator._battery_provenance_total_kwh() == pytest.approx(level / 100 * 27)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(max(0, (level - 89) / 100 * 27))
    assert coordinator._battery_provenance_solar_kwh == pytest.approx(3 * min(1, level / 89))


@pytest.mark.parametrize("data", [None, {}, {"battery_power": 0.0}, {"battery_level": None}])
def test_first_solve_without_measured_soc_preserves_restored_inventory(opt_module, monkeypatch, data):
    clock = {"now": datetime(2026, 10, 31, 10, tzinfo=timezone.utc), "data": data}
    coordinator = _charging_solve_coordinator(opt_module, monkeypatch, clock)
    del coordinator._energy_telemetry_ready  # Exercise the real Tesla readiness path.
    coordinator._battery_provenance_store = _ProvenanceStore(_stored_inventory())
    asyncio.run(coordinator._restore_battery_provenance())
    assert coordinator._energy_telemetry_ready() is True
    _run_to_acquisition_cost(coordinator)
    assert coordinator._battery_provenance_total_kwh() == pytest.approx(24.03)
    assert coordinator._battery_provenance_solar_kwh == pytest.approx(3.0)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(0.0)
    assert clock["acquisition_cost"] == pytest.approx(0.0)
    clock["data"] = {"battery_level": 89.0}
    _run_to_acquisition_cost(coordinator)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(0.0)


def _shutdown_coordinator(opt_module, *, monitoring=False):
    coordinator = _provenance_coordinator(opt_module)
    coordinator._battery_provenance_available = True
    coordinator._battery_provenance_grid_kwh = 24.03
    coordinator._enabled = True
    coordinator._monitoring_mode_active = lambda: monitoring
    coordinator._get_active_force_state = lambda: None
    coordinator._last_executed_action = None
    coordinator._scheduled_ev_no_discharge_active = False
    for attr in ("_polling_task", "_initial_opt_task", "_deferred_restore_task",
                 "_settings_reoptimize_task", "_price_listener_unsub",
                 "_octopus_gate_listener_unsub"):
        setattr(coordinator, attr, None)
    coordinator._executor = SimpleNamespace(stop=AsyncMock(return_value=True))
    coordinator._ev_coordinator = SimpleNamespace(
        stop=AsyncMock(), recover_license_sessions=AsyncMock(return_value=True),
    )
    coordinator._cost_store = SimpleNamespace(async_save=AsyncMock())
    coordinator._cost_data_to_save = lambda: {"charge_kwh": 1.0}
    coordinator._battery_provenance_store.async_save = AsyncMock()
    return coordinator


@pytest.mark.parametrize("failure", ["false", "raise", "ev_false", "ev_raise", "normal_raise", "cancel"])
def test_shutdown_flushes_provenance_when_restore_fails(opt_module, failure):
    coordinator = _shutdown_coordinator(opt_module)
    if failure == "false":
        coordinator._executor.stop.return_value = False
    elif failure in ("raise", "normal_raise"):
        coordinator._executor.stop.side_effect = RuntimeError("Powerwall setup is stopping or was replaced")
    elif failure == "ev_false":
        coordinator._ev_coordinator.recover_license_sessions.return_value = False
    elif failure == "ev_raise":
        coordinator._ev_coordinator.stop.side_effect = RuntimeError("EV stop failed")
    else:
        coordinator._executor.stop.side_effect = asyncio.CancelledError()
    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected) as error:
        asyncio.run(coordinator.disable(licensing=failure != "normal_raise"))
    if failure in ("false", "raise", "ev_false"):
        assert str(error.value) == "optimizer_restore_pending"
        coordinator._ev_coordinator.recover_license_sessions.assert_awaited_once()
    assert coordinator._enabled is False
    coordinator._cost_store.async_save.assert_awaited_once_with({"charge_kwh": 1.0})
    coordinator._battery_provenance_store.async_save.assert_awaited_once_with(
        coordinator._battery_provenance_data_to_save())


def test_shutdown_cost_write_failure_does_not_skip_provenance(opt_module):
    coordinator = _shutdown_coordinator(opt_module)
    coordinator._cost_store.async_save.side_effect = OSError("disk write failed")
    with pytest.raises(OSError):
        asyncio.run(coordinator.disable())
    coordinator._executor.stop.assert_awaited_once_with(restore_normal=True)
    coordinator._battery_provenance_store.async_save.assert_awaited_once()
    assert coordinator._enabled is False


def test_shutdown_provenance_write_failure_remains_visible(opt_module):
    coordinator = _shutdown_coordinator(opt_module)
    coordinator._battery_provenance_store.async_save.side_effect = OSError("provenance write failed")
    with pytest.raises(OSError, match="provenance write failed"):
        asyncio.run(coordinator.disable())
    coordinator._cost_store.async_save.assert_awaited_once()
    assert coordinator._enabled is False


@pytest.mark.parametrize("monitoring", [False, True])
def test_shutdown_success_flushes_without_changing_monitoring_contract(opt_module, monitoring):
    coordinator = _shutdown_coordinator(opt_module, monitoring=monitoring)
    asyncio.run(coordinator.disable())
    coordinator._executor.stop.assert_awaited_once_with(restore_normal=not monitoring)
    coordinator._battery_provenance_store.async_save.assert_awaited_once()
