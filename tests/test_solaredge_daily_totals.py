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

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = Store
    ha_dt.utcnow = lambda: datetime(2026, 5, 29, 1, 0, 0)
    ha_dt.now = lambda: datetime(2026, 5, 29, 12, 0, 0)

    sys.modules["homeassistant"] = ha_root
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


class _FakeController:
    def __init__(self, statuses: list[dict]) -> None:
        self.statuses = statuses

    def get_status(self) -> dict:
        return self.statuses.pop(0)


def _new_coordinator(statuses: list[dict]) -> SolarEdgeEnergyCoordinator:
    coordinator = SolarEdgeEnergyCoordinator.__new__(SolarEdgeEnergyCoordinator)
    coordinator.hass = types.SimpleNamespace(data={})
    coordinator.data = None
    coordinator._entry_id = "entry-1"
    coordinator._controller = _FakeController(statuses)
    coordinator._energy_acc = _FakeEnergyAccumulator()
    coordinator._validated = True
    coordinator._daily_total_store = sys.modules[
        "homeassistant.helpers.storage"
    ].Store()
    coordinator._daily_total_baselines_restored = False
    coordinator._daily_total_baseline_date = None
    coordinator._daily_total_import_baseline = None
    coordinator._daily_total_export_baseline = None
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
