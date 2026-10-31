"""Tesla-owned charging is fixed site demand, counted once per vehicle."""

import asyncio
import ast
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).parents[1] / "custom_components/power_sync"
SPEC = importlib.util.spec_from_file_location("tesla_schedule_forecast_test", ROOT / "optimization/tesla_schedules.py")
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)
VIN = "5YJ3E1EA7KF000001"
OTHER = "5YJ3E1EA7KF000002"
NOW = datetime(2026, 10, 21, 10, tzinfo=ZoneInfo("Australia/Brisbane"))


def schedule(**changes):
    return dict(enabled=True, start_enabled=True, start_time=600,
                end_enabled=True, end_time=780, days_of_week=127,
                latitude=-27.47, longitude=153.03, **changes)


def vehicle(schedules=None, **charge):
    return {
        "state": "online",
        "charge_schedule_data": {"charge_schedules": schedules if schedules is not None else [schedule()]},
        "charge_state": {"battery_level": 20, "charge_limit_soc": 80,
                         "charge_current_request": 30, "charger_voltage": 240,
                         "charger_phases": 1, "timestamp": NOW.timestamp() * 1000,
                         **charge},
    }


def test_rate_authority_requires_current_explicit_home_window():
    window = m.active_native_window(data=vehicle(), now=NOW,
        schedule_read_at=NOW, latitude=-27.47, longitude=153.03)
    assert window and window.start == NOW.timestamp() and window.end == (NOW+timedelta(hours=3)).timestamp()
    assert window.read_at == NOW.timestamp()


@pytest.mark.parametrize('changes', [dict(start_enabled=False), dict(end_enabled=False),
    dict(one_time=True), dict(latitude=0), dict(enabled=False), dict(start_time=600.5),
    dict(end_time=600), dict(days_of_week=0)])
def test_forecast_window_is_not_rate_authority(changes):
    rule = schedule(); rule.update(changes)
    assert m.active_native_window(data=vehicle([rule]), now=NOW,
        schedule_read_at=NOW, latitude=-27.47, longitude=153.03) is None


def test_rate_window_expires_and_recurring_overnight_is_allowed():
    rule = schedule(); rule.update(start_time=1380, end_time=660)
    window = m.active_native_window(data=vehicle([rule]), now=NOW,
        schedule_read_at=NOW, latitude=-27.47, longitude=153.03)
    assert window and window.end == (NOW+timedelta(hours=1)).timestamp()
    assert m.active_native_window(data=vehicle(), now=NOW+timedelta(hours=3),
        schedule_read_at=NOW+timedelta(hours=3), latitude=-27.47, longitude=153.03) is None
    assert m.active_native_window(data=vehicle(), now=NOW,
        schedule_read_at=NOW-timedelta(seconds=901), latitude=-27.47, longitude=153.03) is None


def forecast(data=None, *, start=NOW, count=5, minutes=60, capacity=60):
    return m.forecast_vehicle(vehicle_id=VIN, data=data or vehicle(),
        timestamps=[start + timedelta(minutes=minutes * i) for i in range(count)],
        interval_minutes=minutes, latitude=-27.47, longitude=153.03,
        capacity_kwh=capacity)


def test_disabled_or_unconfigured_smart_charging_still_has_native_demand():
    native = forecast()
    smart, prices, plans = m.merge_native_schedules(
        native={VIN: native}, smart={}, price={}, plans=None,
        canonical={}, interval_minutes=60)
    assert smart[VIN] == pytest.approx([7200, 7200, 7200, 0, 0])
    assert prices == {} and plans is None


def test_identical_and_partly_overlapping_tesla_windows_are_one_charge():
    windows = [schedule(), schedule(), {**schedule(), "start_time": 720, "end_time": 840}]
    assert forecast(vehicle(windows)).power_w == pytest.approx([7200] * 4 + [0])


def test_same_free_period_in_both_controllers_is_not_doubled():
    native = forecast()
    plan = SimpleNamespace(vehicle_id="ble_car", energy_needed_kwh=40, charge_efficiency=1)
    other = SimpleNamespace(vehicle_id=OTHER, energy_needed_kwh=10)
    smart, prices, plans = m.merge_native_schedules(
        native={VIN: native}, smart={"ble_car": [7200] * 4 + [0]},
        price={VIN: [7200] * 3 + [0, 0], OTHER: [2000] * 5},
        plans=[plan, other], canonical={VIN: VIN, "ble_car": VIN}, interval_minutes=60)
    assert smart[VIN] == pytest.approx([7200] * 4 + [0])
    assert "ble_car" not in smart and VIN not in prices
    assert prices[OTHER] == [2000] * 5
    assert plans == [other]


def test_both_controllers_share_one_remaining_energy_budget():
    native = forecast(vehicle(battery_level=68, charge_limit_soc=80))  # 8 kWh AC
    smart, _, _ = m.merge_native_schedules(
        native={VIN: native}, smart={VIN: [0, 7200, 7200, 7200, 0]},
        price={}, plans=[], canonical={}, interval_minutes=60)
    assert sum(smart[VIN]) / 1000 == pytest.approx(8)
    assert smart[VIN] == pytest.approx([7200, 800, 0, 0, 0])


def test_different_cars_at_same_time_are_separate_demand():
    native = forecast()
    smart, _, _ = m.merge_native_schedules(
        native={VIN: native, OTHER: m.TeslaScheduleForecast(OTHER, native.power_w, 40)},
        smart={}, price={}, plans=None, canonical={}, interval_minutes=60)
    assert sum(series[0] for series in smart.values()) == pytest.approx(14400)


@pytest.mark.parametrize("changes", [
    {"enabled": False}, {"latitude": -33.86, "longitude": 151.21},
    {"days_of_week": 2}, {"start_time": float("nan")},
    {"start_enabled": False, "end_enabled": False},
])
def test_disabled_work_wrong_weekday_and_invalid_schedules_are_ignored(changes):
    assert forecast(vehicle([{**schedule(), **changes}])) is None


def test_weekday_bitmask_uses_sunday_as_bit_zero():
    sunday = NOW.replace(day=25)
    assert forecast(vehicle([{**schedule(), "days_of_week": 1}]), start=sunday)
    assert forecast(vehicle([{**schedule(), "days_of_week": 2}]), start=sunday) is None


def test_overnight_windows_keep_previous_day_and_local_timezone():
    start = NOW.replace(hour=23)
    windows = [{**schedule(), "start_time": 1380, "end_time": 120, "days_of_week": 8}]
    assert forecast(vehicle(windows), start=start).power_w == pytest.approx([7200] * 3 + [0, 0])


def test_end_only_is_finish_by_and_start_only_is_open_ended():
    end_only = [{**schedule(), "start_enabled": False, "end_time": 780}]
    data = vehicle(end_only, battery_level=68, charge_limit_soc=80)
    result = forecast(data)
    assert result.power_w == pytest.approx([0, 800, 7200, 0, 0])
    start_only = [{**schedule(), "end_enabled": False}]
    assert forecast(vehicle(start_only)).power_w == pytest.approx([7200] * 5)


def test_partial_slot_and_target_limit_energy_are_preserved():
    windows = [{**schedule(), "start_time": 615, "end_time": 645}]
    assert forecast(vehicle(windows), minutes=30).power_w[:2] == pytest.approx([3600, 3600])
    assert forecast(vehicle(charge_limit_soc=20)) is None
    assert forecast(vehicle(battery_level=None)) is None


def test_current_away_or_disconnected_state_does_not_erase_future_home_windows():
    data = vehicle()
    data['drive_state'] = {'latitude': -33.86, 'longitude': 151.21, 'timestamp': NOW.timestamp() * 1000}
    assert forecast(data).power_w == pytest.approx([0, 7200, 7200, 0, 0])
    assert forecast(vehicle(charging_state='Disconnected')).power_w == pytest.approx([0, 7200, 7200, 0, 0])


def test_online_away_unplugged_vehicle_can_refresh_a_future_home_schedule():
    async def run():
        data = vehicle(charging_state='Disconnected')
        data['drive_state'] = {'latitude': -33.86, 'longitude': 151.21, 'timestamp': NOW.timestamp() * 1000}
        hass, api, _ = hass_for(data)
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        native = reader.forecasts(timestamps=[NOW + timedelta(hours=i) for i in range(5)], interval_minutes=60,
            latitude=-27.47, longitude=153.03, capacities={}, power_limits={}, now=NOW)
        assert len(api.calls) == 1
        assert native[VIN].power_w == pytest.approx([0, 7200, 7200, 0, 0])
    asyncio.run(run())


def test_one_time_schedule_does_not_repeat():
    data = vehicle([{**schedule(), "one_time": True}], battery_level=0, charge_limit_soc=100)
    result = forecast(data, count=48)
    assert sum(result.power_w[24:]) == 0


def test_rolling_horizon_never_reschedules_a_cached_one_time_event():
    reader = m.TeslaScheduleReader()
    reader.cache[VIN] = (NOW, vehicle([{**schedule(), 'one_time': True}]))
    result = reader.forecasts(timestamps=[NOW + timedelta(days=1)], interval_minutes=60,
        latitude=-27.47, longitude=153.03, capacities={}, power_limits={},
        now=NOW + timedelta(hours=23))
    assert result == {}


@pytest.mark.parametrize('month,day,end,expected', [
    (4, 5, 180, [7200, 7200, 7200, 0]),  # repeated local hour
    (10, 4, 240, [7200, 7200, 0, 0]),  # skipped local hour
])
def test_daylight_saving_counts_real_slot_energy(month, day, end, expected):
    local = ZoneInfo('Australia/Sydney')
    start = datetime(2026, month, day, 1, tzinfo=local).astimezone(timezone.utc)
    timestamps = [(start + timedelta(hours=index)).astimezone(local) for index in range(4)]
    result = m.forecast_vehicle(vehicle_id=VIN,
        data=vehicle([{**schedule(), 'start_time': 60, 'end_time': end}]),
        timestamps=timestamps, interval_minutes=60, latitude=-27.47, longitude=153.03, capacity_kwh=60)
    assert result.power_w == pytest.approx(expected)


def test_confirmed_native_solar_schedules_only_replenish_to_lower_limit():
    reader = m.TeslaScheduleReader()
    reader.cache[VIN] = (NOW, vehicle(battery_level=50))
    kwargs = dict(timestamps=[NOW], interval_minutes=60, latitude=-27.47,
                  longitude=153.03, capacities={}, power_limits={}, now=NOW)
    assert reader.forecasts(**kwargs)
    assert reader.forecasts(**kwargs, native_solar_settings={
        VIN: {'enabled': True, 'lowerChargeLimit': 50, 'upperChargeLimit': 80}}) == {}


class API:
    def __init__(self, data):
        self.data, self.calls, self.fail = data, [], False

    async def vehicle_data(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("unavailable")
        return {"response": self.data}

    async def _request(self, method, path, params):
        self.calls.append({'method': method, 'path': path, 'params': params})
        if self.fail:
            raise RuntimeError('unavailable')
        return {'response': self.data}


def hass_for(data, *, api=None, provider='teslemetry'):
    api = api or API(data)
    coordinator = SimpleNamespace(api=api, data=data, last_update_success=True)
    obj = SimpleNamespace(vin=VIN, coordinator=coordinator)
    entries = SimpleNamespace(async_entries=lambda domain: [SimpleNamespace(
        runtime_data=SimpleNamespace(vehicles=[obj]))] if domain == provider else [])
    return SimpleNamespace(config_entries=entries), api, coordinator


@pytest.mark.parametrize('provider', ['teslemetry', 'tesla_fleet'])
def test_reader_fetches_schedule_data_without_wake_and_throttles(provider):
    async def run():
        hass, api, _ = hass_for(vehicle(), provider=provider)
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        await reader.refresh(hass, NOW + timedelta(minutes=5))
        if provider == 'teslemetry':
            assert api.calls == [{'method': 'GET', 'path': f'api/1/vehicles/{VIN}/vehicle_data',
                                 'params': {'endpoints': 'charge_schedule_data;charge_state', 'use_cache': 'false'}}]
        else:
            assert api.calls == [{"endpoints": ["charge_schedule_data", "charge_state"]}]
        assert VIN in reader.cache
    asyncio.run(run())


def test_sleeping_vehicle_is_never_polled_or_woken():
    async def run():
        hass, api, _ = hass_for({**vehicle(), "state": "asleep"})
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        assert api.calls == [] and reader.cache == {}
    asyncio.run(run())


def test_disabled_native_planning_skips_paid_reads_and_cached_demand_for_only_that_car():
    async def run():
        hass, api, _ = hass_for(vehicle())
        reader = m.TeslaScheduleReader()
        reader.cache[VIN] = (NOW, vehicle())
        await reader.refresh(hass, NOW, excluded_vins=frozenset({VIN}))
        assert not api.calls and VIN in reader.cache
        other = '5YJ3E1EA7KF000002'
        reader.cache[other] = (NOW, vehicle())
        forecasts = reader.forecasts(timestamps=[NOW + timedelta(hours=i) for i in range(5)], interval_minutes=60,
            latitude=-27.47, longitude=153.03, capacities={}, power_limits={}, now=NOW,
            excluded_vins=frozenset({VIN}))
        assert set(forecasts) == {other}
        assert sum(forecasts[other].power_w) > 0
        await reader.refresh(hass, NOW)
        assert len(api.calls) == 1 and 'confirmed' in reader.diagnostics[VIN].lower()
    asyncio.run(run())


def test_failed_read_retains_cache_but_confirmed_empty_removes_schedule():
    async def run():
        hass, api, _ = hass_for(vehicle())
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        api.fail = True
        await reader.refresh(hass, NOW + timedelta(minutes=15))
        assert reader.cache[VIN][1]["charge_schedule_data"]["charge_schedules"]
        api.fail, api.data = False, vehicle([])
        await reader.refresh(hass, NOW + timedelta(minutes=30))
        assert reader.cache[VIN][1]["charge_schedule_data"]["charge_schedules"] == []
    asyncio.run(run())


@pytest.fixture
def fleet_reader(monkeypatch):
    # The optional Fleet SDK deliberately uses BaseException, not Exception.
    class TeslaFleetError(BaseException):
        pass

    class VehicleOffline(TeslaFleetError):
        pass

    package = ModuleType("tesla_fleet_api")
    package.__path__ = []
    exceptions = ModuleType("tesla_fleet_api.exceptions")
    exceptions.TeslaFleetError = TeslaFleetError
    monkeypatch.setitem(sys.modules, package.__name__, package)
    monkeypatch.setitem(sys.modules, exceptions.__name__, exceptions)
    spec = importlib.util.spec_from_file_location("tesla_schedule_fleet_errors_test", SPEC.origin)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module, VehicleOffline


@pytest.mark.parametrize("provider", ["tesla_fleet", "teslemetry"])
def test_sdk_offline_read_preserves_cache_and_continues_other_vehicles(fleet_reader, provider):
    module, VehicleOffline = fleet_reader

    class OfflineAPI(API):
        async def vehicle_data(self, **kwargs):
            self.calls.append(kwargs)
            raise VehicleOffline("went asleep after the provider's online sample")

        async def _request(self, *args):
            return await self.vehicle_data()

    async def run():
        failing_api = OfflineAPI(vehicle())
        hass, _, _ = hass_for(vehicle(), api=failing_api, provider=provider)
        entries = hass.config_entries.async_entries(provider)
        other_api = API(vehicle())
        entries[0].runtime_data.vehicles.append(SimpleNamespace(
            vin=OTHER, coordinator=SimpleNamespace(data=vehicle(), api=other_api)))
        hass.config_entries.async_entries = lambda domain: entries if domain == provider else []
        reader = module.TeslaScheduleReader()
        cached = (NOW - timedelta(minutes=1), vehicle())
        reader.cache[VIN] = cached

        await reader.refresh(hass, NOW)

        assert reader.cache[VIN] is cached
        assert "unavailable" in reader.diagnostics[VIN]
        assert reader.cache[OTHER][0] == NOW
        assert len(other_api.calls) == 1
        await reader.refresh(hass, NOW + timedelta(minutes=5))
        assert len(failing_api.calls) == len(other_api.calls) == 1

        # A failed read must not renew the old schedule's freshness.
        await reader.refresh(hass, NOW + timedelta(days=1, minutes=1))
        assert VIN not in reader.cache

    asyncio.run(run())


@pytest.mark.parametrize("error", [asyncio.CancelledError, SystemExit, KeyboardInterrupt])
def test_schedule_read_propagates_cancellation_and_process_exit(fleet_reader, error):
    module, _ = fleet_reader

    class InterruptedAPI(API):
        async def vehicle_data(self, **kwargs):
            raise error()

    async def run():
        hass, _, _ = hass_for(vehicle(), api=InterruptedAPI(vehicle()), provider="tesla_fleet")
        with pytest.raises(error):
            await module.TeslaScheduleReader().refresh(hass, NOW)

    asyncio.run(run())


def test_sdk_offline_read_does_not_terminate_periodic_replanning(fleet_reader):
    module, VehicleOffline = fleet_reader
    source = ast.parse((ROOT / "optimization/coordinator.py").read_text())
    polling = next(node for node in ast.walk(source)
                   if isinstance(node, ast.AsyncFunctionDef) and node.name == "_schedule_polling_loop")
    polling.decorator_list = []

    async def run():
        class OfflineAPI(API):
            async def vehicle_data(self, **kwargs):
                self.calls.append(kwargs)
                raise VehicleOffline()

        class AccessLost(asyncio.CancelledError):
            pass

        # Run the real polling loop and reader; replace hardware/solver work.
        namespace = {"asyncio": asyncio, "_LOGGER": __import__("logging").getLogger(__name__),
                     "OptimizerAccessLost": AccessLost}
        exec(compile(ast.Module(body=[polling], type_ignores=[]), "<polling>", "exec"), namespace)
        hass, api, _ = hass_for(vehicle(), api=OfflineAPI(vehicle()), provider="tesla_fleet")
        reader = module.TeslaScheduleReader()
        state = SimpleNamespace(_enabled=True, _executor=None, _initial_opt_task=None,
            _should_restore_pre_idle_backup_reserve_from_polling=lambda: False,
            _seconds_until_next_interval=lambda: 0,
            _seconds_until_initial_optimization_allowed=lambda: 0,
            async_set_updated_data=lambda data: None, get_api_data=lambda: {})
        completed = []

        async def cached():
            pass

        async def solve(**kwargs):
            await reader.refresh(hass, NOW + timedelta(minutes=5 * len(completed)))
            completed.append(kwargs["execution_trigger"])
            if len(completed) == 2:
                state._enabled = False

        state._execute_cached_current_action_if_changed = cached
        state._run_optimization = solve
        await namespace["_schedule_polling_loop"](state)
        assert completed == ["poll", "poll"]
        assert len(api.calls) == 1  # Keep the paid-read throttle across cycles.

    asyncio.run(run())


def test_stale_charge_state_or_schedule_cache_never_produces_demand():
    reader = m.TeslaScheduleReader()
    reader.cache[VIN] = (NOW, vehicle())
    kwargs = dict(timestamps=[NOW], interval_minutes=60, latitude=-27.47,
                  longitude=153.03, capacities={}, power_limits={})
    assert reader.forecasts(**kwargs, now=NOW)
    assert reader.forecasts(**kwargs, now=NOW + timedelta(minutes=16))
    assert 'last known' in reader.diagnostics[VIN]
    assert reader.forecasts(**kwargs, now=NOW + timedelta(days=2)) == {}
    reader.cache[VIN] = (NOW - timedelta(days=2), vehicle())
    assert reader.forecasts(**kwargs, now=NOW) == {}


@pytest.mark.parametrize('held', [False, True])
@pytest.mark.parametrize('excluded', [False, True])
def test_real_coordinator_handoff_resolves_capacity_power_and_native_ownership(monkeypatch, held, excluded):
    package = ModuleType('tesla_schedule_coordinator_probe')
    package.__path__ = [str(ROOT)]
    optimization = ModuleType(package.__name__ + '.optimization')
    optimization.__path__ = [str(ROOT / 'optimization')]
    automations = ModuleType(package.__name__ + '.automations')
    automations.__path__ = [str(ROOT / 'automations')]
    for module in (package, optimization, automations):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(sys.modules, optimization.__name__ + '.tesla_schedules', m)
    capacity_name = automations.__name__ + '.ev_vehicle_capacity'
    capacity_spec = importlib.util.spec_from_file_location(capacity_name, ROOT / 'automations/ev_vehicle_capacity.py')
    capacity = importlib.util.module_from_spec(capacity_spec)
    monkeypatch.setitem(sys.modules, capacity_name, capacity)
    capacity_spec.loader.exec_module(capacity)
    source = ast.parse((ROOT / 'optimization/coordinator.py').read_text())
    function = next(node for node in ast.walk(source) if isinstance(node, ast.FunctionDef)
                    and node.name == '_incorporate_tesla_schedule_forecasts')
    namespace = {'__package__': optimization.__name__,
                 'dt_util': SimpleNamespace(DEFAULT_TIME_ZONE=NOW.tzinfo, now=lambda: NOW)}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<coordinator>', 'exec'), namespace)
    reader = m.TeslaScheduleReader()
    reader.cache[VIN] = (NOW, vehicle())
    holds = {VIN: {'enabled': True, 'lowerChargeLimit': 50, 'upperChargeLimit': 80}} if held else {}
    config = {'vehicle_id': VIN, 'battery_capacity_kwh': 72, 'max_amps': 16, 'voltage': 240, 'phases': 1}
    coordinator = SimpleNamespace(
        _tesla_schedule_reader=reader, _entry=SimpleNamespace(entry_id='one'),
        _config=SimpleNamespace(interval_minutes=60),
        _pending_ev_charge_plan=[SimpleNamespace(vehicle_id=VIN, energy_needed_kwh=48, charge_efficiency=1)],
        _canonical_ev_loadpoint_ids=lambda ids: {key: key for key in ids},
        _price_timestamps=lambda n: [NOW + timedelta(hours=i) for i in range(n)],
        hass=SimpleNamespace(config=SimpleNamespace(latitude=-27.47, longitude=153.03), data={
            'power_sync': {'one': {'automation_store': SimpleNamespace(_data={'vehicle_charging_configs': [config]})}},
            'power_sync_native_solar': {'one': SimpleNamespace(holds=holds)},
            'power_sync_tesla_schedule_planning': {'one': SimpleNamespace(excluded_vins=frozenset({VIN}) if excluded else frozenset())},
        }),
    )
    smart, prices, ids = namespace[function.name](coordinator, 4, {VIN: [3840] * 4}, {})
    if excluded:
        assert ids == set() and prices == {}
        assert smart == ({} if held else {VIN: [3840] * 4})
        assert len(coordinator._pending_ev_charge_plan) == (0 if held else 1)
        assert coordinator._last_tesla_schedule_forecast['vehicles'] == {}
        assert coordinator._last_tesla_schedule_forecast['excluded_vins'] == [VIN]
        return
    assert ids == {VIN} and prices == {} and coordinator._pending_ev_charge_plan == []
    assert smart[VIN] == pytest.approx([3840, 3840, 3840, 0] if held else [3840] * 4)
    diagnostic = coordinator._last_tesla_schedule_forecast['vehicles'][VIN]
    assert coordinator._last_tesla_schedule_forecast['entry_id'] == 'one'
    assert coordinator._last_tesla_schedule_forecast['timestamps'][0] == NOW.isoformat()
    assert coordinator._last_tesla_schedule_forecast['interval_minutes'] == 60
    assert diagnostic['battery_capacity_kwh'] == 72 and diagnostic['battery_capacity_source'] == 'manual'
    assert diagnostic['energy_needed_kwh'] == pytest.approx(24 if held else 48)


@pytest.mark.parametrize('voltage', [0, 2, -1, 89, 301, None, float('nan')])
def test_unplugged_voltage_uses_nominal_supply_not_32_watts(voltage):
    data = vehicle(charger_voltage=voltage, charge_current_request=16,
                   charging_state='Disconnected', battery_level=58, charge_limit_soc=70)
    result = forecast(data, capacity=78, count=30)
    assert max(result.power_w) == pytest.approx(3680)
    assert sum(result.power_w) / 1000 == pytest.approx(10.4)


def test_configured_three_phase_supply_is_used_when_unplugged():
    result = m.forecast_vehicle(vehicle_id=VIN,
        data=vehicle(charger_voltage=2, charger_phases=None, charge_current_request=16),
        timestamps=[NOW + timedelta(hours=i) for i in range(5)], interval_minutes=60,
        latitude=-27.47, longitude=153.03, capacity_kwh=60,
        nominal_voltage=240, nominal_phases=3, max_power_kw=11.52)
    assert max(result.power_w) == pytest.approx(11520)


def provider_snapshot(*, age=60, flat=False, schedules=None):
    data = vehicle(schedules, charging_state='Disconnected')
    data['state'] = 'offline'
    data['charge_schedule_data']['timestamp_seconds'] = (NOW - timedelta(seconds=age)).timestamp()
    if flat:
        return {f'{section}_{key}' if isinstance(value, dict) else section: item
                for section, value in data.items()
                for key, item in (value.items() if isinstance(value, dict) else [('', value)])}
    return data


@pytest.mark.parametrize('flat', [False, True])
def test_restart_seeds_fresh_provider_schedules_without_waking_car(flat):
    async def run():
        data = provider_snapshot(flat=flat)
        hass, api, _ = hass_for(data)
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        assert not api.calls
        assert VIN in reader.cache
        assert reader.cache[VIN][0] == NOW - timedelta(seconds=60)
        result = reader.forecasts(timestamps=[NOW + timedelta(hours=i) for i in range(5)],
            interval_minutes=60, latitude=-27.47, longitude=153.03,
            capacities={}, power_limits={}, now=NOW)
        assert max(result[VIN].power_w) == 7200
    asyncio.run(run())


@pytest.mark.parametrize('age', [86401, -60])
def test_provider_schedule_cache_requires_bounded_timestamp(age):
    async def run():
        hass, api, _ = hass_for(provider_snapshot(age=age))
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        assert not api.calls and reader.cache == {}
    asyncio.run(run())


def test_fresh_provider_empty_schedules_replace_older_cache_without_a_read():
    async def run():
        hass, api, _ = hass_for(provider_snapshot(schedules=[]))
        reader = m.TeslaScheduleReader()
        reader.cache[VIN] = (NOW - timedelta(hours=1), vehicle())
        await reader.refresh(hass, NOW)
        assert not api.calls
        assert reader.cache[VIN][1]['charge_schedule_data']['charge_schedules'] == []
    asyncio.run(run())


def test_older_provider_schedules_do_not_replace_newer_confirmed_empty_read():
    async def run():
        hass, api, _ = hass_for(provider_snapshot())
        reader = m.TeslaScheduleReader()
        reader.cache[VIN] = (NOW, vehicle([]))
        await reader.refresh(hass, NOW)
        assert not api.calls
        assert reader.cache[VIN][1]['charge_schedule_data']['charge_schedules'] == []
    asyncio.run(run())


def test_fresh_schedule_cannot_make_stale_battery_data_usable():
    async def run():
        data = provider_snapshot()
        data['charge_state']['timestamp'] = (NOW - timedelta(days=2)).timestamp() * 1000
        hass, api, _ = hass_for(data)
        reader = m.TeslaScheduleReader()
        await reader.refresh(hass, NOW)
        assert not api.calls
        assert VIN in reader.cache
        assert not reader.forecasts(timestamps=[NOW], interval_minutes=60,
            latitude=-27.47, longitude=153.03, capacities={}, power_limits={}, now=NOW)
    asyncio.run(run())
