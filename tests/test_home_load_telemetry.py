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
from pathlib import Path
import sys
import types

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

from power_sync.coordinator import DOMAIN, TeslaEnergyCoordinator  # noqa: E402
from power_sync.const import (  # noqa: E402
    POWERSYNC_AUTH_START_URL,
    TESLA_PROVIDER_POWERSYNC,
    TESLA_PROVIDER_TESLEMETRY,
)


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
    assert headers["X-PowerSync-Control-Mode"] == expected_mode
    assert int(headers["X-PowerSync-Control-Observed-At"]) > 0


def test_non_powersync_tesla_headers_do_not_leak_cloud_ownership_metadata():
    coordinator = _tesla_header_subject(
        monitoring_mode=False,
        provider=TESLA_PROVIDER_TESLEMETRY,
    )

    headers = coordinator._tesla_headers("teslemetry_test_token")

    assert "X-PowerSync-Client-Type" not in headers
    assert "X-PowerSync-Control-Mode" not in headers
    assert "X-PowerSync-Control-Observed-At" not in headers


def test_powersync_copy_paste_auth_url_is_explicitly_home_assistant():
    assert "client_type=home_assistant" in POWERSYNC_AUTH_START_URL
    assert "control_mode=actuating" in POWERSYNC_AUTH_START_URL
