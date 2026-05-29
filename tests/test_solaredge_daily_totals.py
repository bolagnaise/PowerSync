"""Regression tests for SolarEdge daily import/export total-counter deltas."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules.setdefault("power_sync", _ps)


def _install_homeassistant_stubs() -> None:
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
        hass.recorder_calls.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "entity_ids": entity_ids,
            }
        )
        return hass.recorder_history

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = Store
    ha_dt.utcnow = lambda: datetime(2026, 5, 29, 1, 0, 0)
    ha_dt.now = lambda: datetime(2026, 5, 29, 12, 0, 0)
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


_install_homeassistant_stubs()

from power_sync.coordinator import SolarEdgeEnergyCoordinator  # noqa: E402


class _FakeEnergyAccumulator:
    _last_update = True

    async def async_restore(self) -> None:
        return None

    def update(self, *args) -> None:
        return None

    def as_dict(self) -> dict:
        return {
            "pv_today_kwh": 0,
            "grid_import_today_kwh": 0,
            "grid_export_today_kwh": 0,
            "charge_today_kwh": 0,
            "discharge_today_kwh": 0,
            "load_today_kwh": 0,
            "import_cost_today": 0,
            "export_earnings_today": 0,
        }


class _FakeStore:
    def __init__(self) -> None:
        self.data = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.data = data


class _FakeController:
    def __init__(self, statuses: list[dict]) -> None:
        self.statuses = statuses

    def get_status(self) -> dict:
        return self.statuses.pop(0)


class _FakeHistoryState:
    def __init__(
        self,
        state: str,
        last_changed: datetime,
        unit: str = "kWh",
    ) -> None:
        self.state = state
        self.last_changed = last_changed
        self.attributes = {"unit_of_measurement": unit}


def _new_coordinator(statuses: list[dict]) -> SolarEdgeEnergyCoordinator:
    coordinator = SolarEdgeEnergyCoordinator.__new__(SolarEdgeEnergyCoordinator)
    coordinator.hass = types.SimpleNamespace(data={}, recorder_history={}, recorder_calls=[])
    coordinator.data = None
    coordinator._entry_id = "entry-1"
    coordinator._controller = _FakeController(statuses)
    coordinator._energy_acc = _FakeEnergyAccumulator()
    coordinator._validated = True
    coordinator._daily_total_store = _FakeStore()
    coordinator._daily_total_baselines_restored = False
    coordinator._daily_total_baseline_date = None
    coordinator._daily_total_import_baseline = None
    coordinator._daily_total_export_baseline = None
    coordinator._daily_total_recorder_baselines_checked = False
    return coordinator


def test_solaredge_daily_import_export_are_derived_from_lifetime_totals():
    statuses = [
        {
            "solar_power": 3.0,
            "grid_power": 1.0,
            "battery_power": 0.0,
            "load_power": 4.0,
            "battery_level": 70,
            "total_grid_import_kwh": 1000.0,
            "total_grid_export_kwh": 500.0,
        },
        {
            "solar_power": 3.0,
            "grid_power": 1.0,
            "battery_power": 0.0,
            "load_power": 4.0,
            "battery_level": 70,
            "total_grid_import_kwh": 1002.345,
            "total_grid_export_kwh": 501.25,
        },
    ]
    coordinator = _new_coordinator(statuses)

    first = asyncio.run(coordinator._async_update_data())
    second = asyncio.run(coordinator._async_update_data())

    assert first["energy_summary"]["grid_import_today_kwh"] == 0.0
    assert first["energy_summary"]["grid_export_today_kwh"] == 0.0
    assert second["energy_summary"]["grid_import_today_kwh"] == pytest.approx(2.345)
    assert second["energy_summary"]["grid_export_today_kwh"] == pytest.approx(1.25)
    assert coordinator._daily_total_store.data["import_baseline_kwh"] == 1000.0
    assert coordinator._daily_total_store.data["export_baseline_kwh"] == 500.0


def test_solaredge_daily_total_baseline_restores_after_restart():
    coordinator = _new_coordinator(
        [
            {
                "solar_power": 0.0,
                "grid_power": 0.0,
                "battery_power": 0.0,
                "load_power": 0.0,
                "battery_level": 70,
                "total_grid_import_kwh": 1010.0,
                "total_grid_export_kwh": 505.5,
            }
        ]
    )
    coordinator._daily_total_store.data = {
        "date": "2026-05-29",
        "import_baseline_kwh": 1000.0,
        "export_baseline_kwh": 500.0,
    }

    data = asyncio.run(coordinator._async_update_data())

    assert data["energy_summary"]["grid_import_today_kwh"] == pytest.approx(10.0)
    assert data["energy_summary"]["grid_export_today_kwh"] == pytest.approx(5.5)


def test_solaredge_daily_total_baseline_uses_recorder_midnight_state():
    coordinator = _new_coordinator(
        [
            {
                "solar_power": 0.0,
                "grid_power": 0.0,
                "battery_power": 0.0,
                "load_power": 0.0,
                "battery_level": 70,
                "total_grid_import_kwh": 9625.716,
                "total_grid_export_kwh": 150.25,
                "total_grid_import_entity_id": "sensor.solaredge_m1_imported_kwh",
                "total_grid_export_entity_id": "sensor.solaredge_m1_exported_kwh",
            }
        ]
    )
    coordinator.hass.recorder_history = {
        "sensor.solaredge_m1_imported_kwh": [
            _FakeHistoryState("9614.650", datetime(2026, 5, 28, 23, 59, 30)),
            _FakeHistoryState("9614.700", datetime(2026, 5, 29, 0, 0, 30)),
            _FakeHistoryState("9625.716", datetime(2026, 5, 29, 12, 0, 0)),
        ],
        "sensor.solaredge_m1_exported_kwh": [
            _FakeHistoryState("150.000", datetime(2026, 5, 28, 23, 59, 30)),
            _FakeHistoryState("150.250", datetime(2026, 5, 29, 12, 0, 0)),
        ],
    }

    data = asyncio.run(coordinator._async_update_data())

    assert data["energy_summary"]["grid_import_today_kwh"] == pytest.approx(11.066)
    assert data["energy_summary"]["grid_export_today_kwh"] == pytest.approx(0.25)
    assert coordinator._daily_total_store.data["import_baseline_kwh"] == pytest.approx(9614.65)
    assert coordinator._daily_total_store.data["export_baseline_kwh"] == pytest.approx(150.0)
    assert coordinator.hass.recorder_calls[0]["entity_ids"] == ["sensor.solaredge_m1_imported_kwh"]


def test_solaredge_daily_total_recorder_corrects_existing_midday_baseline():
    coordinator = _new_coordinator(
        [
            {
                "solar_power": 0.0,
                "grid_power": 0.0,
                "battery_power": 0.0,
                "load_power": 0.0,
                "battery_level": 70,
                "total_grid_import_kwh": 9625.716,
                "total_grid_export_kwh": 150.25,
                "total_grid_import_entity_id": "sensor.solaredge_m1_imported_kwh",
                "total_grid_export_entity_id": "sensor.solaredge_m1_exported_kwh",
            }
        ]
    )
    coordinator._daily_total_store.data = {
        "date": "2026-05-29",
        "import_baseline_kwh": 9622.721,
        "export_baseline_kwh": 150.25,
    }
    coordinator.hass.recorder_history = {
        "sensor.solaredge_m1_imported_kwh": [
            _FakeHistoryState("9614.650", datetime(2026, 5, 28, 23, 59, 30)),
            _FakeHistoryState("9625.716", datetime(2026, 5, 29, 12, 0, 0)),
        ],
        "sensor.solaredge_m1_exported_kwh": [
            _FakeHistoryState("150.000", datetime(2026, 5, 28, 23, 59, 30)),
            _FakeHistoryState("150.250", datetime(2026, 5, 29, 12, 0, 0)),
        ],
    }

    data = asyncio.run(coordinator._async_update_data())

    assert data["energy_summary"]["grid_import_today_kwh"] == pytest.approx(11.066)
    assert data["energy_summary"]["grid_export_today_kwh"] == pytest.approx(0.25)
    assert coordinator._daily_total_store.data["import_baseline_kwh"] == pytest.approx(9614.65)
    assert coordinator._daily_total_store.data["export_baseline_kwh"] == pytest.approx(150.0)
