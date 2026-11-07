"""Ticket #451: reloads preserve measured battery inventory and pending saves."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
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


def _paired_local_solve(opt_module, monkeypatch):
    """Use real source selection with independently clocked cloud/local data."""
    clock = {"now": datetime(2026, 11, 4, 1, 40, tzinfo=timezone.utc),
             "monotonic": 1000.0,
             "data": {"battery_level": 50.0, "battery_power": -10.0,
                      "grid_power": 14.0, "solar_power": 3.0,
                      "load_power": 4.0, "raw_home_load_power": 7.0,
                      "ev_power": 3.0, "firmware": "cloud-metadata"}}
    coordinator = _charging_solve_coordinator(opt_module, monkeypatch, clock)
    coordinator._get_energy_data = opt_module.OptimizationCoordinator._get_energy_data.__get__(coordinator)
    coordinator._entry.data["powerwall_local_paired"] = True
    coordinator.energy_coordinator = SimpleNamespace(data=clock["data"])
    local = SimpleNamespace(
        data=SimpleNamespace(soc=50.0, battery_w=-10_000.0, grid_w=14_000.0,
                             solar_w=3000.0, load_w=7000.0,
                             observed_monotonic=1000.0),
        last_success_monotonic=1000.0, last_success_ts=clock["now"].timestamp(),
        last_update_success=True,
    )
    coordinator.hass.data["power_sync"] = {
        coordinator.entry_id: {"powerwall_local": {"coordinator": local}}
    }
    monkeypatch.setattr(opt_module.dt_util, "utcnow", lambda: clock["now"])
    time_module = SimpleNamespace(**vars(opt_module.time))
    time_module.monotonic = lambda: clock["monotonic"]
    monkeypatch.setattr(opt_module, "time", time_module)
    clock["data"]["last_update"] = clock["now"]
    return coordinator, clock, local


def test_paired_optimizer_uses_one_local_sample_and_keeps_cloud_metadata(opt_module, monkeypatch):
    coordinator, clock, local = _paired_local_solve(opt_module, monkeypatch)
    local.data.soc = 54.99
    local.data.battery_w = 4500.0
    local.data.grid_w = 0.0
    local.data.solar_w = 2500.0
    data = coordinator._get_energy_data()
    assert data["battery_level"] == 54.99
    assert data["battery_power"] == 4.5
    assert data["grid_power"] == 0.0
    assert data["solar_power"] == 2.5
    assert data["load_power"] == 4.0  # Gross local load minus the observed EV once.
    assert data["firmware"] == "cloud-metadata"
    assert clock["data"]["battery_level"] == 50.0
    assert data["last_update"] == clock["now"]
    clock["now"] += timedelta(seconds=10)
    clock["monotonic"] += 10
    assert coordinator._get_energy_data()["last_update"] == data["last_update"]


def test_stepped_cloud_soc_cannot_dilute_fresh_local_measured_origin(opt_module, monkeypatch):
    """Ticket #144: changed, delayed SOC at a charge/discharge transition."""
    coordinator, clock, local = _paired_local_solve(opt_module, monkeypatch)
    _run_to_acquisition_cost(coordinator)
    coordinator._battery_provenance_available = True
    coordinator._battery_provenance_grid_kwh = 13.5
    coordinator._battery_provenance_unknown_kwh = 0.0
    coordinator._battery_provenance_grid_cost = 1.35
    coordinator._track_actual_cost()
    energy, cost = 13.5, 1.35
    cloud_soc = 50.0
    for index, power in enumerate([-10.0, -10.0, -10.0, 4.5, 4.5, -10.0]):
        clock["now"] += timedelta(seconds=60)
        clock["monotonic"] += 60
        if index in (2, 4):
            cloud_soc = energy / 27 * 100
        delta = -power / 60
        cost = cost + delta * 0.093 if delta > 0 else cost * (energy + delta) / energy
        energy += delta
        clock["data"].update(battery_level=cloud_soc, battery_power=power,
                             grid_power=max(0.0, -power), solar_power=0.0,
                             last_update=clock["now"])
        local.data.soc = energy / 27 * 100
        local.data.battery_w = power * 1000
        local.data.grid_w = max(0.0, -power) * 1000
        local.data.solar_w = 0.0
        local.data.observed_monotonic = clock["monotonic"]
        local.last_success_monotonic = clock["monotonic"]
        local.last_success_ts = clock["now"].timestamp()
        _run_to_acquisition_cost(coordinator)
        _run_to_acquisition_cost(coordinator)
        assert coordinator._battery_provenance_total_kwh() == pytest.approx(energy)
        assert coordinator._battery_provenance_grid_kwh == pytest.approx(energy)
        assert coordinator._battery_provenance_unknown_kwh == pytest.approx(0.0)
        assert coordinator._battery_provenance_grid_cost == pytest.approx(cost)
        assert clock["acquisition_cost"] == pytest.approx(cost / energy)


def test_local_cloud_source_switches_do_not_settle_old_samples_twice(opt_module, monkeypatch):
    coordinator, clock, local = _paired_local_solve(opt_module, monkeypatch)
    coordinator._entry.data["powerwall_local_paired"] = False
    _run_to_acquisition_cost(coordinator)
    coordinator._track_actual_cost()
    start = clock["now"]
    clock["now"] += timedelta(seconds=10)
    clock["monotonic"] += 10
    coordinator._entry.data["powerwall_local_paired"] = True
    local.data.observed_monotonic += 8  # Request start precedes its success.
    local.last_success_monotonic += 10
    local.last_success_ts = clock["now"].timestamp()
    local.data.soc = (13.5 + 10 * 8 / 3600) / 27 * 100
    _run_to_acquisition_cost(coordinator)
    assert coordinator._actual_charge_kwh_today == pytest.approx(10 * 8 / 3600)
    assert coordinator._last_cost_tracking_time == start + timedelta(seconds=8)
    local.last_update_success = False
    clock["data"]["last_update"] = start + timedelta(seconds=5)
    _run_to_acquisition_cost(coordinator)
    assert coordinator._actual_charge_kwh_today == pytest.approx(10 * 8 / 3600)
    assert coordinator._last_cost_tracking_time == start + timedelta(seconds=8)
    clock["now"] += timedelta(seconds=20)
    clock["monotonic"] += 20
    clock["data"]["last_update"] = start + timedelta(seconds=25)
    _run_to_acquisition_cost(coordinator)
    assert coordinator._actual_charge_kwh_today == pytest.approx(10 * 25 / 3600)
    local.last_update_success = True
    _run_to_acquisition_cost(coordinator)  # Local request is still fresh but older.
    assert coordinator._actual_charge_kwh_today == pytest.approx(10 * 25 / 3600)
    assert coordinator._last_cost_tracking_time == start + timedelta(seconds=25)


@pytest.mark.parametrize("boundary", [
    "unpaired", "other_brand", "failed", "missing", "stale", "stale_snapshot",
    "future_snapshot", "no_timestamp", "no_wall_timestamp", "future_wall_timestamp",
    "missing_soc", "nan_soc", "invalid_soc", "missing_power", "nan_power", "missing_grid",
    "missing_solar", "missing_load",
])
def test_local_optimizer_source_falls_back_atomically(opt_module, monkeypatch, boundary):
    coordinator, clock, local = _paired_local_solve(opt_module, monkeypatch)
    local.data.soc = 54.99
    local.data.battery_w = 4500.0
    if boundary == "unpaired":
        coordinator._entry.data["powerwall_local_paired"] = False
    elif boundary == "other_brand":
        coordinator.battery_system = "sigenergy"
    elif boundary == "failed":
        local.last_update_success = False
    elif boundary == "missing":
        local.data = None
    elif boundary == "stale":
        local.last_success_monotonic -= 31
    elif boundary == "stale_snapshot":
        local.data.observed_monotonic -= 31
    elif boundary == "future_snapshot":
        local.data.observed_monotonic += 1
    elif boundary == "no_timestamp":
        local.data.observed_monotonic = None
    elif boundary == "no_wall_timestamp":
        local.last_success_ts = None
    elif boundary == "future_wall_timestamp":
        local.last_success_ts += 60
    else:
        attr, value = {
            "missing_soc": ("soc", None), "nan_soc": ("soc", float("nan")),
            "invalid_soc": ("soc", 101.0), "missing_power": ("battery_w", None),
            "nan_power": ("battery_w", float("nan")), "missing_grid": ("grid_w", None),
            "missing_solar": ("solar_w", None), "missing_load": ("load_w", None),
        }[boundary]
        setattr(local.data, attr, value)
    data = coordinator._get_energy_data()
    assert data["battery_level"] == 50.0
    assert data["battery_power"] == -10.0
    assert data["last_update"] == clock["data"]["last_update"]


@pytest.mark.parametrize("power", [-6.0, 6.0])
def test_optimizer_solves_preserve_origin_until_soc_catches_up(opt_module, monkeypatch, power):
    """Ticket #144: a solve must not undo measured-interval SOC deferral."""
    clock = {"now": datetime(2026, 11, 1, 10, tzinfo=timezone.utc),
             "data": {"battery_level": 50.0, "battery_power": power,
                      "solar_power": max(0.0, -power), "grid_power": 0.0}}
    coordinator = _charging_solve_coordinator(opt_module, monkeypatch, clock)
    _run_to_acquisition_cost(coordinator)
    coordinator._battery_provenance_available = True
    coordinator._battery_provenance_solar_kwh = 4.5
    coordinator._battery_provenance_grid_kwh = 4.5
    coordinator._battery_provenance_grid_cost = 0.9
    coordinator._battery_provenance_unknown_kwh = 4.5
    coordinator._track_actual_cost()
    solar, grid, unknown, cost = 4.5, 4.5, 4.5, 0.9
    for _ in range(3):
        clock["now"] += timedelta(seconds=60)
        if power < 0:
            solar += 0.1
        else:
            ratio = (solar + grid + unknown - 0.1) / (solar + grid + unknown)
            solar, grid, unknown, cost = [v * ratio for v in (solar, grid, unknown, cost)]
        _run_to_acquisition_cost(coordinator)
        # A second solve at the identical timestamp has no new interval.
        _run_to_acquisition_cost(coordinator)
        assert coordinator._battery_provenance_solar_kwh == pytest.approx(solar)
        assert coordinator._battery_provenance_grid_kwh == pytest.approx(grid)
        assert coordinator._battery_provenance_unknown_kwh == pytest.approx(unknown)
        assert coordinator._battery_provenance_grid_cost == pytest.approx(cost)
    clock["data"]["battery_level"] = (solar + grid + unknown) / 27 * 100
    _run_to_acquisition_cost(coordinator)
    assert coordinator._battery_provenance_total_kwh() == pytest.approx(solar + grid + unknown)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(unknown)
    assert clock["acquisition_cost"] == pytest.approx((cost + unknown * 0.2986) / (solar + grid + unknown))


@pytest.mark.parametrize("boundary", ["gap", "startup", "capacity", "changed_soc", "zero_interval"])
def test_pending_measured_origin_keeps_authoritative_boundaries(opt_module, boundary):
    coordinator = _provenance_coordinator(opt_module)
    coordinator._battery_provenance_available = True
    coordinator._battery_provenance_solar_kwh = 5.0
    coordinator._battery_provenance_last_soc = 0.5
    coordinator._battery_provenance_last_capacity_wh = 10_000
    interval = dict(solar_charge_kwh=0.1, grid_charge_kwh=0.0,
                    grid_charge_cost=0.0, discharge_kwh=0.0,
                    current_soc=0.5, capacity_wh=10_000, telemetry_gap=False)
    coordinator._record_battery_provenance_interval(**interval)
    assert coordinator._battery_provenance_total_kwh() == pytest.approx(5.1)
    if boundary in ("gap", "zero_interval"):
        interval.update(solar_charge_kwh=0.0, telemetry_gap=boundary == "gap")
        coordinator._record_battery_provenance_interval(**interval)
    else:
        coordinator._reconcile_battery_provenance(
            0.52 if boundary == "changed_soc" else 0.5,
            12_000 if boundary == "capacity" else 10_000,
            reason="startup" if boundary == "startup" else "optimizer_run",
        )
    expected = {"gap": 5.0, "startup": 5.0, "capacity": 6.0,
                "changed_soc": 5.2, "zero_interval": 5.1}[boundary]
    assert coordinator._battery_provenance_total_kwh() == pytest.approx(expected)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(max(0, expected - 5.1))
    assert coordinator._battery_provenance_soc_pending is (boundary == "zero_interval")


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


class _MeasuredEnergySource:
    def __init__(self):
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def publish(self):
        for listener in list(self.listeners):
            listener()


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("charging_first", [True, False])
def test_measured_updates_preserve_both_directions_between_solves(
    opt_module, monkeypatch, local, charging_first
):
    """Ticket #154: a new mode must not price the preceding five minutes."""
    clock = {"now": datetime(2026, 11, 7, 14, tzinfo=timezone.utc),
             "data": {"battery_level": 50.0, "battery_power": -10.0,
                      "grid_power": 10.0, "solar_power": 0.0}}
    coordinator = _charging_solve_coordinator(opt_module, monkeypatch, clock)
    source = _MeasuredEnergySource()
    cloud = _MeasuredEnergySource() if local else source
    coordinator.energy_coordinator = cloud
    coordinator.hass.data["power_sync"] = {
        coordinator.entry_id: {"powerwall_local": {"coordinator": source} if local else {}}
    }
    coordinator._restore_cost_data = AsyncMock()
    coordinator._restore_battery_provenance = AsyncMock()
    coordinator._battery_provenance_available = True
    coordinator._battery_provenance_grid_kwh = 13.5
    coordinator._battery_provenance_grid_cost = 1.35
    assert asyncio.run(_setup_persistence_tail(opt_module)(coordinator))
    coordinator._track_actual_cost()  # First observation seeds the clock.
    energy = 13.5
    charge, discharge, grid_cost = 0.0, 0.0, 0.0
    for index in range(1, 103):  # 306 s, with 3 s observed samples.
        power = -10.0 if (index <= 100) == charging_first else 2.75
        clock["now"] += timedelta(seconds=3)
        clock["data"].update(battery_power=power, grid_power=max(0.0, -power),
                             last_update=clock["now"])
        delta = power * 3 / 3600
        energy -= delta
        charge += max(0.0, -delta)
        discharge += max(0.0, delta)
        grid_cost += max(0.0, -delta) * 0.093
        clock["data"]["battery_level"] = energy / 27 * 100
        source.publish()
        cloud.publish()  # Same snapshot cannot be counted twice.
    _run_to_acquisition_cost(coordinator)
    assert coordinator._actual_charge_kwh_today == pytest.approx(charge)
    assert coordinator._actual_discharge_kwh_today == pytest.approx(discharge)
    assert coordinator._actual_grid_charge_cost_today == pytest.approx(grid_cost)
    assert coordinator._actual_import_kwh_today == pytest.approx(charge)
    assert coordinator._battery_provenance_unknown_kwh == pytest.approx(0.0, abs=1e-9)
    assert coordinator._battery_provenance_grid_kwh == pytest.approx(energy)
    coordinator._stop_cost_tracking_listeners()
    assert not source.listeners
    assert not cloud.listeners


def test_cost_sampling_rebinds_replaced_local_source_without_duplicate_listeners(opt_module):
    coordinator = _shutdown_coordinator(opt_module)
    cloud, first, replacement = (_MeasuredEnergySource() for _ in range(3))
    coordinator.energy_coordinator = cloud
    runtime = {"powerwall_local": {"coordinator": first}}
    coordinator.hass.data["power_sync"] = {coordinator.entry_id: runtime}
    calls = []
    coordinator._track_actual_cost = lambda **kw: calls.append(kw)
    coordinator._sync_cost_tracking_listeners()
    coordinator._sync_cost_tracking_listeners()
    assert len(cloud.listeners) == len(first.listeners) == 1
    late_callback = first.listeners[0]
    runtime["powerwall_local"]["coordinator"] = replacement
    coordinator._sync_cost_tracking_listeners()
    assert not first.listeners
    assert len(replacement.listeners) == 1
    cloud.publish()
    replacement.publish()
    assert calls == [{"force": True}, {"force": True}]
    # Shutdown removes callbacks even if hardware restoration subsequently fails.
    coordinator._executor.stop.side_effect = RuntimeError("restore unavailable")
    with pytest.raises(RuntimeError, match="restore unavailable"):
        asyncio.run(coordinator.disable())
    assert not cloud.listeners
    assert not replacement.listeners
    late_callback()
    assert len(calls) == 2
    coordinator._sync_cost_tracking_listeners()
    assert not cloud.listeners


def test_cost_sampling_registers_shared_source_only_once(opt_module):
    coordinator = _shutdown_coordinator(opt_module)
    source = _MeasuredEnergySource()
    coordinator.energy_coordinator = source
    coordinator.hass.data["power_sync"] = {
        coordinator.entry_id: {"powerwall_local": {"coordinator": source}}
    }
    coordinator._sync_cost_tracking_listeners()
    assert len(source.listeners) == 1
    coordinator._stop_cost_tracking_listeners()
    assert not source.listeners



def test_cost_sampling_failure_does_not_abort_other_telemetry_listeners(opt_module):
    coordinator = _shutdown_coordinator(opt_module)
    source = _MeasuredEnergySource()
    coordinator.energy_coordinator = source
    coordinator._sync_cost_tracking_listeners()
    delivered = []
    source.async_add_listener(lambda: delivered.append("telemetry delivered"))

    def failed_accounting(**_kwargs):
        raise ValueError("invalid measured power")

    coordinator._track_actual_cost = failed_accounting
    source.publish()
    assert delivered == ["telemetry delivered"]
    coordinator._stop_cost_tracking_listeners()
