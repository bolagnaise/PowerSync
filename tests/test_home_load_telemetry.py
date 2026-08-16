"""Regression tests for home_load telemetry contamination bugs.

home_load must be house consumption only — never battery-charge or EV power
that PowerSync already accounts for elsewhere. Two brand paths were found to
leak other power flows into the load estimator's training data:

  OB-24: Tesla's local-Powerwall outage fallback (used when Tesla cloud
         returns an empty live_status and a local gateway is paired) reported
         the raw gateway load without subtracting EV (Wall Connector) power,
         unlike the main cloud path which always subtracts it.

  OB-25: SAJ H2 falls back to the raw ``gridPower`` sensor for load_power when
         ``TotalLoadPower`` isn't exposed by the upstream saj_h2_modbus
         integration. gridPower is the net grid leg, not house consumption,
         so it bakes in battery-charge power during grid charging and reads
         ~0 during self-consumption.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
import types
from unittest.mock import AsyncMock

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


# ---------------------------------------------------------------------------
# OB-24: Tesla local-Powerwall outage fallback must exclude EV power
# ---------------------------------------------------------------------------


def _install_coordinator_stubs() -> None:
    """Stub just enough of homeassistant.* for coordinator.py to import.

    Mirrors the stub set used by tests/test_solaredge_daily_totals.py, which
    exercises the same coordinator.py module.
    """
    ha_components = types.ModuleType("homeassistant.components")
    ha_recorder = types.ModuleType("homeassistant.components.recorder")
    ha_recorder_history = types.ModuleType("homeassistant.components.recorder.history")
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    class DataUpdateCoordinator:
        def __init__(self, hass, *args, **kwargs) -> None:
            self.hass = hass
            self.data = None

    class Store:
        def __init__(self, *args, **kwargs) -> None:
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    class FakeRecorder:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def get_significant_states(hass, start_time, end_time, entity_ids):
        return {}

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = Store
    from datetime import datetime as _dt

    ha_dt.utcnow = lambda: _dt(2026, 7, 8, 1, 0, 0)
    ha_dt.now = lambda: _dt(2026, 7, 8, 12, 0, 0)
    ha_recorder.get_instance = lambda hass: FakeRecorder()
    ha_recorder_history.get_significant_states = get_significant_states

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.recorder"] = ha_recorder
    sys.modules["homeassistant.components.recorder.history"] = ha_recorder_history
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_aiohttp_client
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt

    ps = types.ModuleType("power_sync")
    ps.__path__ = [str(COMPONENT_ROOT)]
    sys.modules.setdefault("power_sync", ps)


_install_coordinator_stubs()
sys.modules.pop("power_sync.coordinator", None)

from power_sync.coordinator import (  # noqa: E402
    DOMAIN,
    TeslaEnergyCoordinator,
    _fresh_site_ev_load,
    _mapped_tesla_other_charger_power_kw,
    _update_energy_accumulator_with_ev_load,
)
from power_sync.const import (  # noqa: E402
    POWERSYNC_AUTH_START_URL,
    powersync_auth_start_url,
    TESLA_PROVIDER_POWERSYNC,
    TESLA_PROVIDER_TESLEMETRY,
)


def test_fresh_site_ev_load_prefers_complete_provider_neutral_total():
    snapshot = types.SimpleNamespace(
        power_kw=9.2,
        observed_at=datetime(2026, 7, 8, 0, 59, 30),
        quality=types.SimpleNamespace(value="complete"),
        components=(object(), object()),
        unavailable_active_keys=(),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )

    assert _fresh_site_ev_load(hass, "entry-1", 7.2) == (9.2, True)


def test_stale_site_ev_load_fails_closed_instead_of_using_partial_fallback():
    snapshot = types.SimpleNamespace(
        power_kw=9.2,
        observed_at=datetime(2026, 7, 8, 0, 58, 0),
        quality=types.SimpleNamespace(value="complete"),
        components=(object(),),
        unavailable_active_keys=(),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )

    assert _fresh_site_ev_load(hass, "entry-1", 7.2) == (7.2, False)


def test_stale_site_ev_load_uses_same_vehicle_direct_meter_fallback():
    """A current direct meter can replace its stale canonical vehicle reading."""
    vehicle_key = "vehicle:5yjtest0000000001"
    snapshot = types.SimpleNamespace(
        power_kw=10.2,
        observed_at=datetime(2026, 7, 8, 0, 58, 0),
        quality=types.SimpleNamespace(value="complete"),
        components=(
            types.SimpleNamespace(
                physical_load_key=vehicle_key,
                power_kw=10.2,
                active=True,
            ),
        ),
        unavailable_active_keys=(),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )

    assert _fresh_site_ev_load(
        hass,
        "entry-1",
        10.8,
        fallback_by_physical_key={vehicle_key: 10.8},
    ) == (10.8, True)


def test_incomplete_site_ev_load_uses_same_vehicle_direct_meter_fallback():
    """A direct meter can fill its own missing vehicle without hiding another EV."""
    snapshot = types.SimpleNamespace(
        power_kw=2.4,
        observed_at=datetime(2026, 7, 8, 0, 59, 30),
        quality=types.SimpleNamespace(value="incomplete"),
        components=(
            types.SimpleNamespace(
                physical_load_key="ocpp:garage:1",
                power_kw=2.4,
                active=True,
            ),
        ),
        unavailable_active_keys=("vehicle:5yjtest0000000001",),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )

    power_kw, complete = _fresh_site_ev_load(
        hass,
        "entry-1",
        10.8,
        fallback_by_physical_key={"vehicle:5yjtest0000000001": 10.8},
    )

    assert power_kw == pytest.approx(13.2)
    assert complete is True


def test_incomplete_site_ev_load_still_fails_closed_for_distinct_missing_ev():
    """A Wall Connector reading must not cover a separate unmeasured charger."""
    snapshot = types.SimpleNamespace(
        power_kw=10.8,
        observed_at=datetime(2026, 7, 8, 0, 59, 30),
        quality=types.SimpleNamespace(value="incomplete"),
        components=(
            types.SimpleNamespace(
                physical_load_key="vehicle:5yjtest0000000001",
                power_kw=10.8,
                active=True,
            ),
        ),
        unavailable_active_keys=("ocpp:garage:1",),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )

    assert _fresh_site_ev_load(
        hass,
        "entry-1",
        10.8,
        fallback_by_physical_key={"vehicle:5yjtest0000000001": 10.8},
    ) == (10.8, False)


def test_daily_home_energy_uses_the_same_site_ev_total():
    snapshot = types.SimpleNamespace(
        power_kw=2.0,
        observed_at=datetime(2026, 7, 8, 0, 59, 30),
        quality=types.SimpleNamespace(value="complete"),
        components=(object(),),
        unavailable_active_keys=(),
    )
    hass = types.SimpleNamespace(
        data={DOMAIN: {"entry-1": {"observed_ev_load_snapshot": snapshot}}}
    )
    calls = []
    accumulator = types.SimpleNamespace(update=lambda *args: calls.append(args))

    assert _update_energy_accumulator_with_ev_load(
        accumulator,
        hass,
        "entry-1",
        4.0,
        1.0,
        0.0,
        5.0,
        0.3,
        0.1,
    )
    assert calls == [(4.0, 1.0, 0.0, 3.0, 0.3, 0.1)]


class _FakeEnergyAccumulator:
    def update(self, *args) -> None:
        return None

    def as_dict(self) -> dict:
        return {}


class _FakeLocalPowerwallCoordinator:
    """Stand-in for powerwall_local.coordinator.PowerwallLocalCoordinator.

    Only exposes the surface _local_powerwall_energy_data() touches: the raw
    snapshot on .data, and _observed_ev_power_w() -- the same "observed EV
    power" signal that PowerwallLocalCoordinator.snapshot_as_api() subtracts
    from the raw gateway load (powerwall_local/coordinator.py:258-261).
    """

    def __init__(self, snap, ev_power_w: float) -> None:
        self.data = snap
        self._ev_power_w = ev_power_w

    def _observed_ev_power_w(self) -> float:
        return self._ev_power_w


def _new_tesla_coordinator(local_coordinator) -> TeslaEnergyCoordinator:
    coordinator = TeslaEnergyCoordinator.__new__(TeslaEnergyCoordinator)
    entry_id = "tesla-entry-1"
    coordinator._entry_id = entry_id
    coordinator.hass = types.SimpleNamespace(
        data={
            DOMAIN: {
                entry_id: {
                    "powerwall_local": {"coordinator": local_coordinator},
                }
            }
        }
    )
    coordinator._energy_acc = _FakeEnergyAccumulator()
    coordinator._firmware = None
    coordinator._lifetime_totals = None
    coordinator._last_valid_battery_level_pct = None
    return coordinator


def test_tesla_local_powerwall_fallback_excludes_ev_power_from_load():
    """OB-24: outage fallback must subtract observed EV power like the cloud path does."""
    snap = types.SimpleNamespace(
        solar_w=1200.0,
        grid_w=0.0,
        battery_w=-2100.0,
        load_w=10700.0,
        grid_status="SystemGridConnected",
        soc=62.0,
        total_pack_full_wh=None,
        total_pack_remaining_wh=None,
    )
    local_coordinator = _FakeLocalPowerwallCoordinator(snap, ev_power_w=7100.0)
    coordinator = _new_tesla_coordinator(local_coordinator)

    data = coordinator._local_powerwall_energy_data()

    assert data is not None
    # Raw gateway load (10700 W) minus observed EV (7100 W) = 3600 W = 3.6 kW.
    assert data["load_power"] == pytest.approx(3.6)
    assert data["load_power"] != pytest.approx(10.7)
    assert data["ev_power"] == pytest.approx(7.1)


def test_tesla_local_powerwall_fallback_clamps_load_at_zero_and_defaults_ev_to_zero():
    """Defensive: EV power missing/absent must not crash or go negative."""
    snap = types.SimpleNamespace(
        solar_w=0.0,
        grid_w=0.0,
        battery_w=0.0,
        load_w=500.0,
        grid_status="SystemGridConnected",
        soc=40.0,
        total_pack_full_wh=None,
        total_pack_remaining_wh=None,
    )

    class _NoEvMethodCoordinator:
        def __init__(self, snap) -> None:
            self.data = snap

    coordinator = _new_tesla_coordinator(_NoEvMethodCoordinator(snap))

    data = coordinator._local_powerwall_energy_data()

    assert data is not None
    assert data["load_power"] == pytest.approx(0.5)
    assert data["ev_power"] == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("Active", "Active"),
        ("SystemGridConnected", "Active"),
        ("Inactive", "Off-Grid"),
        ("Islanded", "Off-Grid"),
        ("Off-Grid", "Off-Grid"),
        ("SystemIslandedActive", "Off-Grid"),
        ("SystemIslandedReady", None),
        ("SystemTransitionToGrid", None),
        ("SystemTransitionToIsland", None),
        ("SystemMicroGridFaulted", None),
        ("SystemWaitForUser", None),
        (None, None),
        ("unexpected-status", None),
    ],
)
def test_tesla_local_powerwall_fallback_grid_status_is_terminal_only(
    raw_status,
    expected,
):
    snap = types.SimpleNamespace(
        solar_w=0.0,
        grid_w=0.0,
        battery_w=0.0,
        load_w=500.0,
        grid_status=raw_status,
        soc=40.0,
        total_pack_full_wh=None,
        total_pack_remaining_wh=None,
    )
    coordinator = _new_tesla_coordinator(
        _FakeLocalPowerwallCoordinator(snap, ev_power_w=0.0)
    )

    data = coordinator._local_powerwall_energy_data()

    assert data is not None
    assert data["grid_status"] == expected


# ---------------------------------------------------------------------------
# OB-25: SAJ H2 must not fall back to raw gridPower for home_load
# ---------------------------------------------------------------------------


def _install_saj_stubs() -> None:
    ha_entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    ha_entity_registry.async_get = lambda hass: hass.entity_registry
    ha_entity_registry.async_entries_for_config_entry = (
        lambda registry, entry_id: registry.entries_for(entry_id)
    )
    sys.modules["homeassistant.helpers.entity_registry"] = ha_entity_registry

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules.setdefault("power_sync.inverters", inverters)


_install_saj_stubs()
sys.modules.pop("power_sync.inverters.saj_h2", None)

from power_sync.inverters.saj_h2 import SajH2BatteryController  # noqa: E402


class _SajFakeState:
    def __init__(self, entity_id: str, state: str):
        self.entity_id = entity_id
        self.state = state
        self.attributes: dict = {}


class _SajFakeStates:
    def __init__(self, states: list[_SajFakeState]):
        self._states = {s.entity_id: s for s in states}

    def get(self, entity_id: str | None):
        return self._states.get(entity_id or "")


class _SajFakeRegistry:
    def __init__(self, entries: dict[str, list[tuple[str, str]]]):
        self._entries = entries

    def entries_for(self, entry_id: str):
        return [
            types.SimpleNamespace(unique_id=unique_id, entity_id=entity_id)
            for unique_id, entity_id in self._entries.get(entry_id, [])
        ]


class _SajFakeHass:
    def __init__(self, states: list[_SajFakeState], registry_entries: dict):
        self.states = _SajFakeStates(states)
        self.entity_registry = _SajFakeRegistry(registry_entries)


def test_saj_h2_load_uses_balance_formula_when_total_load_power_missing():
    """OB-25: with no TotalLoadPower, importing grid power during a battery
    charge must not leak into home_load -- the balance formula
    (solar + battery + grid, signed) nets the charge power out."""
    hass = _SajFakeHass(
        states=[
            _SajFakeState("sensor.saj_battery_soc", "55"),
            _SajFakeState("sensor.saj_battery_power", "2000"),
            _SajFakeState("sensor.saj_direction_battery", "-1"),  # charging
            _SajFakeState("sensor.saj_grid_power", "3000"),  # importing
            _SajFakeState("sensor.saj_solar_power", "0"),
        ],
        registry_entries={
            "saj-entry": [
                ("saj_Bat1SOC", "sensor.saj_battery_soc"),
                ("saj_batteryPower", "sensor.saj_battery_power"),
                ("saj_directionBattery", "sensor.saj_direction_battery"),
                ("saj_gridPower", "sensor.saj_grid_power"),
                ("saj_CT_PVPowerWatt", "sensor.saj_solar_power"),
                # No TotalLoadPower entity registered on this install.
            ]
        },
    )
    controller = SajH2BatteryController(hass, saj_entry_id="saj-entry")
    controller._discover_entities()

    # load_power fell back to the same entity as grid_power (raw gridPower).
    assert controller._entity_map.get("load_power") == "sensor.saj_grid_power"

    status = controller.get_status()

    assert status["grid_power"] == pytest.approx(3.0)
    assert status["battery_power"] == pytest.approx(-2.0)
    assert status["solar_power"] == pytest.approx(0.0)
    # Balance formula: solar(0) + battery(-2.0) + grid(3.0) = 1.0 kW.
    # 2 kW of the 3 kW import is charging the battery -- that must not
    # appear in home_load.
    assert status["load_power"] == pytest.approx(1.0)
    # The raw-gridPower (pre-fix) value would have been 3.0 kW -- assert we
    # are not just re-reporting the unsigned grid magnitude as load.
    assert status["load_power"] != pytest.approx(3.0)


def test_saj_h2_load_uses_total_load_power_when_present():
    """Guard: when TotalLoadPower IS available, keep using it directly (no change)."""
    hass = _SajFakeHass(
        states=[
            _SajFakeState("sensor.saj_battery_soc", "55"),
            _SajFakeState("sensor.saj_battery_power", "2000"),
            _SajFakeState("sensor.saj_direction_battery", "-1"),
            _SajFakeState("sensor.saj_grid_power", "3000"),
            _SajFakeState("sensor.saj_solar_power", "0"),
            _SajFakeState("sensor.saj_total_load_power", "1800"),
        ],
        registry_entries={
            "saj-entry": [
                ("saj_Bat1SOC", "sensor.saj_battery_soc"),
                ("saj_batteryPower", "sensor.saj_battery_power"),
                ("saj_directionBattery", "sensor.saj_direction_battery"),
                ("saj_gridPower", "sensor.saj_grid_power"),
                ("saj_CT_PVPowerWatt", "sensor.saj_solar_power"),
                ("saj_TotalLoadPower", "sensor.saj_total_load_power"),
            ]
        },
    )
    controller = SajH2BatteryController(hass, saj_entry_id="saj-entry")
    controller._discover_entities()

    assert controller._entity_map.get("load_power") == "sensor.saj_total_load_power"

    status = controller.get_status()

    # Direct sensor read (1.8 kW), not the balance formula (which would be 1.0 kW).
    assert status["load_power"] == pytest.approx(1.8)


def _tesla_header_subject(*, monitoring_mode: bool, provider: str):
    entry = types.SimpleNamespace(
        data={"monitoring_mode": False},
        options={"monitoring_mode": monitoring_mode},
    )
    coordinator = TeslaEnergyCoordinator.__new__(TeslaEnergyCoordinator)
    coordinator._entry_id = "header-entry"
    coordinator.api_provider = provider
    coordinator.hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_get_entry=lambda entry_id: entry if entry_id == "header-entry" else None
        )
    )
    return coordinator


@pytest.mark.parametrize(
    ("monitoring_mode", "expected_mode"),
    [(True, "monitoring"), (False, "actuating")],
)
def test_powersync_proxy_headers_report_effective_ha_control_mode(
    monitoring_mode: bool,
    expected_mode: str,
):
    """Cloud ownership uses explicit HA capability, not token type alone."""
    coordinator = _tesla_header_subject(
        monitoring_mode=monitoring_mode,
        provider=TESLA_PROVIDER_POWERSYNC,
    )

    headers = coordinator._tesla_headers("psync_test_token")

    assert headers["X-PowerSync-Client-Type"] == "home_assistant"
    assert headers["X-PowerSync-Client-Instance-Id"] == "header-entry"
    assert headers["X-PowerSync-Control-Mode"] == expected_mode
    assert int(headers["X-PowerSync-Control-Observed-At"]) > 0


def test_non_powersync_tesla_headers_do_not_leak_cloud_ownership_metadata():
    coordinator = _tesla_header_subject(
        monitoring_mode=False,
        provider=TESLA_PROVIDER_TESLEMETRY,
    )

    headers = coordinator._tesla_headers("teslemetry_test_token")

    assert "X-PowerSync-Client-Type" not in headers
    assert "X-PowerSync-Client-Instance-Id" not in headers
    assert "X-PowerSync-Control-Mode" not in headers
    assert "X-PowerSync-Control-Observed-At" not in headers


# ---------------------------------------------------------------------------
# Teslemetry Energy Site SSE must replace healthy live_status REST polling
# ---------------------------------------------------------------------------


class _StreamEnergyAccumulator:
    def __init__(self) -> None:
        self._last_update = datetime(2026, 7, 8, 0, 59, tzinfo=timezone.utc)
        self.updates: list[tuple] = []

    async def async_restore(self) -> None:
        raise AssertionError("The initialized accumulator must not restore")

    def update(self, *args) -> None:
        self.updates.append(args)

    def as_dict(self) -> dict:
        return {"solar_kwh": 1.0}


def _new_stream_tesla_coordinator() -> TeslaEnergyCoordinator:
    coordinator = TeslaEnergyCoordinator.__new__(TeslaEnergyCoordinator)
    entry_id = "stream-entry"
    coordinator.hass = types.SimpleNamespace(
        data={DOMAIN: {entry_id: {}}},
        config_entries=types.SimpleNamespace(async_get_entry=lambda entry_id: None),
    )
    coordinator.site_id = "12345"
    coordinator._entry_id = entry_id
    coordinator.api_provider = TESLA_PROVIDER_TESLEMETRY
    coordinator.data = {"old": True}
    coordinator._energy_acc = _StreamEnergyAccumulator()
    coordinator._lifetime_totals_restored = True
    coordinator._lifetime_totals = None
    coordinator._lifetime_last_fetch = time.monotonic()
    coordinator._lifetime_fetch_failed = False
    coordinator._site_info_cache = {}
    coordinator._site_info_last_fetch = time.monotonic()
    coordinator._site_info_fetch_failed = False
    coordinator._firmware = None
    coordinator._last_valid_battery_level_pct = None
    coordinator._last_grid_status = "Active"
    coordinator._consecutive_failures = 0
    coordinator._failure_streak_start = 0
    coordinator._outage_notified = False
    coordinator._teslemetry_stream_connected = True
    coordinator._teslemetry_stream_last_event = 0
    coordinator._teslemetry_stream_created_at = None
    coordinator._teslemetry_stream_live_status = None
    coordinator._teslemetry_stream_generation = 0
    coordinator._teslemetry_stream_processed_generation = 0
    return coordinator


def test_teslemetry_stream_uses_background_task_bucket():
    """The lifetime SSE reconnect loop must not block HA bootstrap."""

    async def _run() -> None:
        coordinator = _new_stream_tesla_coordinator()
        coordinator._teslemetry_stream = None
        coordinator.session = None
        created: dict[str, list[str | None]] = {
            "normal": [],
            "background": [],
        }

        def _create_task(coroutine, *, name=None, **kwargs):
            created["normal"].append(name)
            return asyncio.create_task(coroutine, name=name)

        def _create_background_task(coroutine, name, eager_start=True):
            created["background"].append(name)
            return asyncio.create_task(coroutine, name=name)

        coordinator.hass.async_create_task = _create_task
        coordinator.hass.async_create_background_task = _create_background_task

        try:
            coordinator.async_start_teslemetry_stream()
            assert created["normal"] == []
            assert created["background"] == [
                "power_sync_teslemetry_sse_12345"
            ]
        finally:
            await coordinator.async_shutdown()

    asyncio.run(_run())


def test_teslemetry_sse_snapshot_maps_directly_and_skips_repeat_rest_poll():
    coordinator = _new_stream_tesla_coordinator()
    refresh_count = 0

    async def _request_refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1

    coordinator.async_request_refresh = _request_refresh
    coordinator._get_current_token = lambda: (_ for _ in ()).throw(
        AssertionError("Healthy SSE data must not fetch a REST token")
    )
    event = {
        "createdAt": "2026-07-08T00:59:30.000Z",
        "site_id": "12345",
        "isCache": True,
        "live_status": {
            "solar_power": 2400,
            "grid_power": -300,
            "battery_power": 1100,
            "load_power": 900,
            "percentage_charged": 82.5,
            "grid_status": "Active",
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert refresh_count == 1
    assert result["solar_power"] == pytest.approx(2.4)
    assert result["grid_power"] == pytest.approx(-0.3)
    assert result["battery_power"] == pytest.approx(1.1)
    assert result["load_power"] == pytest.approx(0.9)
    assert result["battery_level"] == pytest.approx(82.5)
    assert result["last_update"] == datetime(
        2026,
        7,
        8,
        0,
        59,
        30,
        tzinfo=timezone.utc,
    )
    assert len(coordinator._energy_acc.updates) == 1
    assert coordinator._teslemetry_stream_processed_generation == 1

    # The coordinator's existing 15-second timer is now only a health check.
    # Until a new SSE generation arrives, it returns the published snapshot
    # without polling REST or integrating the same sample again.
    coordinator.data = result
    repeated = asyncio.run(coordinator._async_update_data())
    assert repeated is result
    assert len(coordinator._energy_acc.updates) == 1


def test_tesla_coordinator_ignores_non_terminal_grid_status_transitions():
    unknown_statuses = (
        "SystemIslandedReady",
        "SystemTransitionToGrid",
        "SystemTransitionToIsland",
        "SystemMicroGridFaulted",
        "SystemWaitForUser",
        None,
        "unexpected-status",
    )
    for raw_status in unknown_statuses:
        coordinator = _new_stream_tesla_coordinator()

        async def _request_refresh() -> None:
            return None

        coordinator.async_request_refresh = _request_refresh
        coordinator._get_current_token = lambda: (_ for _ in ()).throw(
            AssertionError("Healthy SSE data must not fetch a REST token")
        )
        event = {
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "site_id": "12345",
            "live_status": {
                "solar_power": 0,
                "grid_power": 0,
                "battery_power": 0,
                "load_power": 500,
                "percentage_charged": 82.5,
                "grid_status": raw_status,
            },
        }

        asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
        result = asyncio.run(coordinator._async_update_data())

        assert result["grid_status"] is None
        assert coordinator._last_grid_status == "Active"


def test_tesla_coordinator_notifies_only_terminal_class_changes(monkeypatch):
    coordinator = _new_stream_tesla_coordinator()
    coordinator._last_grid_status = None

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    coordinator._get_current_token = lambda: (_ for _ in ()).throw(
        AssertionError("Healthy SSE data must not fetch a REST token")
    )
    send_push = AsyncMock()
    automations_package = types.ModuleType("power_sync.automations")
    automations_package.__path__ = []
    actions_module = types.ModuleType("power_sync.automations.actions")
    actions_module._send_expo_push = send_push
    monkeypatch.setitem(sys.modules, "power_sync.automations", automations_package)
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)

    async def _publish(raw_status, created_at):
        event = {
            "createdAt": created_at,
            "site_id": "12345",
            "live_status": {
                "solar_power": 0,
                "grid_power": 0,
                "battery_power": 0,
                "load_power": 500,
                "percentage_charged": 82.5,
                "grid_status": raw_status,
            },
        }
        await coordinator._async_handle_teslemetry_stream_event(event)
        result = await coordinator._async_update_data()
        coordinator.data = result
        return result

    first = asyncio.run(_publish("Inactive", "2026-07-08T00:59:30+00:00"))
    assert first["grid_status"] == "Inactive"
    assert coordinator._last_grid_status == "Inactive"
    send_push.assert_not_awaited()

    restored = asyncio.run(_publish("Active", "2026-07-08T00:59:31+00:00"))
    assert restored["grid_status"] == "Active"
    send_push.assert_awaited_once_with(
        coordinator.hass,
        "Grid Power Restored",
        "Grid power has been restored. Your Powerwall is back on-grid.",
    )

    asyncio.run(
        _publish("SystemGridConnected", "2026-07-08T00:59:32+00:00")
    )
    assert send_push.await_count == 1

    asyncio.run(
        _publish("SystemIslandedActive", "2026-07-08T00:59:33+00:00")
    )
    assert send_push.await_count == 2
    assert send_push.await_args.args == (
        coordinator.hass,
        "Grid Outage Detected",
        "Your Powerwall is running off-grid. Grid power is unavailable.",
    )


def test_tesla_explicit_zero_wall_connector_power_suppresses_vehicle_fallback(
    monkeypatch,
):
    """A reporting Wall Connector's zero is authoritative when native charging stops."""
    coordinator = _new_stream_tesla_coordinator()

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    entry = types.SimpleNamespace(entry_id="stream-entry", data={}, options={})
    coordinator.hass.config_entries.async_get_entry = (
        lambda entry_id: entry if entry_id == "stream-entry" else None
    )

    fallback_calls = []

    def _stale_vehicle_status(hass, config_entry):
        fallback_calls.append((hass, config_entry))
        return {"ev_power_kw": 4.0, "ev_soc": 75}

    monkeypatch.setattr(
        sys.modules["power_sync"],
        "_get_ev_vehicle_status",
        _stale_vehicle_status,
        raising=False,
    )
    event = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "site_id": "12345",
        "live_status": {
            "solar_power": 5880,
            "grid_power": -2803,
            "battery_power": 0,
            "load_power": 3077,
            "percentage_charged": 100,
            "grid_status": "Active",
            "wall_connectors": [
                {
                    "wall_connector_state": 4,
                    "wall_connector_power": 0,
                }
            ],
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert fallback_calls == []
    assert result["ev_power"] == pytest.approx(0.0)
    assert result["load_power"] == pytest.approx(3.077)


def test_tesla_direct_wall_connector_fills_incomplete_same_vehicle_snapshot():
    """Ticket #204: valid VIN-scoped Wall Connector power keeps Home Load numeric."""
    coordinator = _new_stream_tesla_coordinator()

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    vin = "5YJTEST0000000001"
    coordinator.hass.data[DOMAIN]["stream-entry"][
        "observed_ev_load_snapshot"
    ] = types.SimpleNamespace(
        power_kw=0.0,
        observed_at=datetime(2026, 7, 8, 0, 59, 30),
        quality=types.SimpleNamespace(value="incomplete"),
        components=(),
        unavailable_active_keys=(f"vehicle:{vin.lower()}",),
    )
    wall_connector = {
        "din": "1529455-42-H--TEST",
        "vin": vin,
        "wall_connector_state": 1,
        "wall_connector_power": 10532.46,
    }
    event = {
        "createdAt": "2026-07-08T00:59:30.000Z",
        "site_id": "12345",
        "live_status": {
            "solar_power": 1732,
            "grid_power": 30677,
            "battery_power": -15075,
            "load_power": 17334,
            "percentage_charged": 86.6,
            "grid_status": "Active",
            "wall_connectors": [wall_connector],
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert result["ev_power"] == pytest.approx(10.53246)
    assert result["raw_home_load_power"] == pytest.approx(17.334)
    assert result["load_power"] == pytest.approx(6.80154)
    assert result["home_load_normalization_quality"] == "complete"
    assert result["ev_power_fallback_by_physical_key"] == {
        f"vehicle:{vin.lower()}": pytest.approx(10.53246)
    }
    assert result["wall_connectors_raw"] == [wall_connector]


def test_tesla_idle_wall_connector_keeps_distinct_mapped_umc_power(monkeypatch):
    """An idle Wall Connector must not hide another mapped Tesla's UMC draw."""
    coordinator = _new_stream_tesla_coordinator()

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    tessy_vin = "5YJTEST0000000001"
    umc_vin = "5YJTEST0000000002"
    entry = types.SimpleNamespace(
        entry_id="stream-entry",
        data={},
        options={
            "tesla_ble_vehicle_mapping": (
                f"{tessy_vin}=garage_ble,{umc_vin}=tesla_ble_second_car"
            )
        },
    )
    coordinator.hass.config_entries.async_get_entry = (
        lambda entry_id: entry if entry_id == "stream-entry" else None
    )
    vehicle_calls = []

    def _vehicle_statuses(hass, config_entry):
        vehicle_calls.append((hass, config_entry))
        return [
            {
                "vehicle_id": tessy_vin,
                "ev_power_kw": 0.0,
                "is_connected": True,
                "is_charging": False,
            },
            {
                "vehicle_id": umc_vin,
                "ev_power_kw": 2.0,
                "is_connected": True,
                "is_charging": True,
            },
        ]

    monkeypatch.setattr(
        sys.modules["power_sync"],
        "_get_ev_vehicles_status",
        _vehicle_statuses,
        raising=False,
    )
    event = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "site_id": "12345",
        "live_status": {
            "solar_power": 0,
            "grid_power": 0,
            "battery_power": 4113,
            "load_power": 4113,
            "percentage_charged": 47,
            "grid_status": "Active",
            "wall_connectors": [
                {
                    "wall_connector_state": 4,
                    "wall_connector_power": 0,
                }
            ],
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert len(vehicle_calls) == 1
    assert result["ev_power"] == pytest.approx(2.0)
    assert result["load_power"] == pytest.approx(2.113)


def test_tesla_idle_connector_override_fails_closed_when_identity_is_ambiguous():
    """Configured multiplicity alone must not revive stale vehicle power."""
    first_vin = "5YJTEST0000000001"
    second_vin = "5YJTEST0000000002"
    entry = types.SimpleNamespace(
        data={},
        options={
            "tesla_ble_vehicle_mapping": (
                f"{first_vin}=garage_ble,{second_vin}=tesla_ble_second_car"
            )
        },
    )
    active_vehicle = {
        "vehicle_id": second_vin,
        "ev_power_kw": 2.0,
        "is_connected": True,
        "is_charging": True,
    }

    assert _mapped_tesla_other_charger_power_kw(
        entry,
        [active_vehicle],
    ) == pytest.approx(0.0)
    assert _mapped_tesla_other_charger_power_kw(
        entry,
        [
            {
                "vehicle_id": first_vin,
                "ev_power_kw": 0.0,
                "is_connected": True,
                "is_charging": False,
            },
            active_vehicle,
        ],
        {second_vin},
    ) == pytest.approx(0.0)


def test_tesla_missing_wall_connector_power_preserves_vehicle_fallback(monkeypatch):
    """Vehicle telemetry remains the fallback when the site reports no connector power."""
    coordinator = _new_stream_tesla_coordinator()

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    entry = types.SimpleNamespace(entry_id="stream-entry", data={}, options={})
    coordinator.hass.config_entries.async_get_entry = (
        lambda entry_id: entry if entry_id == "stream-entry" else None
    )
    monkeypatch.setattr(
        sys.modules["power_sync"],
        "_get_ev_vehicle_status",
        lambda hass, config_entry: {"ev_power_kw": 4.0, "ev_soc": 75},
        raising=False,
    )
    event = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "site_id": "12345",
        "live_status": {
            "solar_power": 7077,
            "grid_power": 0,
            "battery_power": 0,
            "load_power": 7077,
            "percentage_charged": 80,
            "grid_status": "Active",
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert result["ev_power"] == pytest.approx(4.0)
    assert result["load_power"] == pytest.approx(3.077)


@pytest.mark.parametrize(
    ("wall_connectors", "expected_ev_power", "fallback_expected"),
    [
        (
            [
                {"wall_connector_power": 1200},
                {"wall_connector_power": 800},
            ],
            2.0,
            False,
        ),
        ('[{"wall_connector_power": 1200}, {"wall_connector_power": 800}]', 2.0, False),
        (
            '[{"wall_connector_power": 1200}, '
            '{"wall_connector_power": null}, '
            '{"wall_connector_power": 800}]',
            2.0,
            False,
        ),
        (
            [
                {"wall_connector_power": 1200},
                {"wall_connector_power": "not-a-number"},
                {"wall_connector_power": 800},
            ],
            2.0,
            False,
        ),
        ([{"wall_connector_power": "not-a-number"}], 4.0, True),
        ([{"wall_connector_power": True}], 4.0, True),
        ([{"wall_connector_power": 1500}], 1.5, False),
    ],
    ids=(
        "multiple",
        "json-string",
        "json-null",
        "mixed-invalid",
        "invalid",
        "boolean",
        "positive",
    ),
)
def test_tesla_wall_connector_power_preserves_aggregation_and_fallback_boundaries(
    monkeypatch,
    wall_connectors,
    expected_ev_power,
    fallback_expected,
):
    """Valid connector readings aggregate; invalid readings retain vehicle fallback."""
    coordinator = _new_stream_tesla_coordinator()

    async def _request_refresh() -> None:
        return None

    coordinator.async_request_refresh = _request_refresh
    entry = types.SimpleNamespace(entry_id="stream-entry", data={}, options={})
    coordinator.hass.config_entries.async_get_entry = (
        lambda entry_id: entry if entry_id == "stream-entry" else None
    )
    fallback_calls = []

    def _vehicle_status(hass, config_entry):
        fallback_calls.append((hass, config_entry))
        return {"ev_power_kw": 4.0, "ev_soc": 75}

    monkeypatch.setattr(
        sys.modules["power_sync"],
        "_get_ev_vehicle_status",
        _vehicle_status,
        raising=False,
    )
    event = {
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "site_id": "12345",
        "live_status": {
            "solar_power": 7077,
            "grid_power": 0,
            "battery_power": 0,
            "load_power": 7077,
            "percentage_charged": 80,
            "grid_status": "Active",
            "wall_connectors": wall_connectors,
        },
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(event))
    result = asyncio.run(coordinator._async_update_data())

    assert bool(fallback_calls) is fallback_expected
    assert result["ev_power"] == pytest.approx(expected_ev_power)
    assert result["load_power"] == pytest.approx(7.077 - expected_ev_power)


def test_teslemetry_sse_ignores_other_sites_and_out_of_order_replays():
    coordinator = _new_stream_tesla_coordinator()
    refresh_count = 0

    async def _request_refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1

    coordinator.async_request_refresh = _request_refresh
    current = {
        "createdAt": "2026-07-08T00:59:30.000Z",
        "site_id": "12345",
        "live_status": {"grid_power": 100},
    }
    other_site = {
        **current,
        "site_id": "67890",
        "live_status": {"grid_power": 999},
    }
    unrelated_topic = {
        "createdAt": "2026-07-08T00:59:35.000Z",
        "site_id": "12345",
        "site_info": {"backup_reserve_percent": 20},
    }
    older = {
        **current,
        "createdAt": "2026-07-08T00:58:30.000Z",
        "live_status": {"grid_power": 888},
    }

    asyncio.run(coordinator._async_handle_teslemetry_stream_event(current))
    asyncio.run(coordinator._async_handle_teslemetry_stream_event(other_site))
    asyncio.run(
        coordinator._async_handle_teslemetry_stream_event(unrelated_topic)
    )
    asyncio.run(coordinator._async_handle_teslemetry_stream_event(older))

    assert refresh_count == 1
    assert coordinator._teslemetry_stream_generation == 1
    assert coordinator._teslemetry_stream_live_status == {"grid_power": 100}


def test_teslemetry_empty_stream_document_forces_immediate_rest_fallback():
    coordinator = _new_stream_tesla_coordinator()
    coordinator._teslemetry_stream_last_event = time.monotonic()
    coordinator._teslemetry_stream_live_status = {"grid_power": 100}
    refresh_count = 0

    async def _request_refresh() -> None:
        nonlocal refresh_count
        refresh_count += 1

    coordinator.async_request_refresh = _request_refresh

    asyncio.run(
        coordinator._async_handle_teslemetry_stream_event(
            {
                "createdAt": "2026-07-08T00:59:45.000Z",
                "site_id": "12345",
                "live_status": {},
            }
        )
    )

    assert refresh_count == 1
    assert coordinator._teslemetry_stream_last_event == 0
    assert coordinator._fresh_teslemetry_stream_snapshot() is None


def test_teslemetry_stale_cache_replay_keeps_rest_fallback_active():
    coordinator = _new_stream_tesla_coordinator()
    coordinator._teslemetry_stream_last_event = time.monotonic()
    coordinator._teslemetry_stream_created_at = datetime(
        2026,
        7,
        7,
        23,
        0,
        tzinfo=timezone.utc,
    )
    coordinator._teslemetry_stream_live_status = {"grid_power": 100}
    coordinator._teslemetry_stream_generation = 1

    assert coordinator._fresh_teslemetry_stream_snapshot() is None


def test_teslemetry_stream_lifecycle_is_wired_to_entry_setup_and_unload():
    init_source = (COMPONENT_ROOT / "__init__.py").read_text(encoding="utf-8")

    assert "tesla_coordinator.async_start_teslemetry_stream()" in init_source
    assert "await tesla_stream_coordinator.async_shutdown()" in init_source


def test_powersync_copy_paste_auth_url_is_explicitly_home_assistant():
    assert "client_type=home_assistant" in POWERSYNC_AUTH_START_URL
    assert "control_mode=actuating" in POWERSYNC_AUTH_START_URL


def test_powersync_copy_paste_auth_url_carries_stable_config_entry_identity():
    auth_url = powersync_auth_start_url("ha-entry-instance-01")

    assert "client_instance_id=ha-entry-instance-01" in auth_url
    assert "client_type=home_assistant" in auth_url
