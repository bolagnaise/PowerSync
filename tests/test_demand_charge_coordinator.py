"""Demand charge coordinator regression tests."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


class _Clock:
    current = datetime(2026, 6, 1, 12, 30, tzinfo=timezone.utc)


def _install_coordinator_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    class ConfigEntryAuthFailed(Exception):
        pass

    class UpdateFailed(Exception):
        pass

    class DataUpdateCoordinator:
        def __init__(self, hass, logger, name=None, update_interval=None) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None

    class Store:
        saved: dict[str, object] = {}

        def __init__(self, _hass, _version, key) -> None:
            self.key = key

        async def async_load(self):
            return self.saved.get(self.key)

        async def async_save(self, value) -> None:
            self.saved[self.key] = value

    ha_core.HomeAssistant = object
    ha_exceptions.ConfigEntryAuthFailed = ConfigEntryAuthFailed
    ha_update.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update.UpdateFailed = UpdateFailed
    ha_aiohttp.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = Store
    ha_dt.now = lambda *args, **kwargs: _Clock.current
    ha_dt.utcnow = lambda *args, **kwargs: _Clock.current
    ha_util.dt = ha_dt

    ha_helpers.update_coordinator = ha_update
    ha_helpers.aiohttp_client = ha_aiohttp
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.storage = ha_storage
    ha_root.core = ha_core
    ha_root.exceptions = ha_exceptions
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    for name, module in {
        "homeassistant": ha_root,
        "homeassistant.core": ha_core,
        "homeassistant.exceptions": ha_exceptions,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.update_coordinator": ha_update,
        "homeassistant.helpers.aiohttp_client": ha_aiohttp,
        "homeassistant.helpers.dispatcher": ha_dispatcher,
        "homeassistant.helpers.storage": ha_storage,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(ROOT)]
    monkeypatch.setitem(sys.modules, "power_sync", ps_module)
    monkeypatch.delitem(sys.modules, "power_sync.coordinator", raising=False)


def _coordinator_module(monkeypatch: pytest.MonkeyPatch):
    _install_coordinator_stubs(monkeypatch)
    return importlib.import_module("power_sync.coordinator")


def test_peak_demand_tracks_only_billable_demand_window_samples(
    monkeypatch: pytest.MonkeyPatch,
):
    coordinator_module = _coordinator_module(monkeypatch)
    energy = SimpleNamespace(data={"grid_power": 11.8})
    demand = coordinator_module.DemandChargeCoordinator(
        hass=SimpleNamespace(),
        energy_coordinator=energy,
        enabled=True,
        rate=9.0,
        start_time="14:55",
        end_time="21:00",
        days="All Days",
        billing_day=1,
    )

    data = asyncio.run(demand._async_update_data())
    assert data["in_peak_period"] is False
    assert data["grid_import_power_kw"] == 11.8
    assert data["peak_demand_kw"] == 0.0
    assert data["estimated_cost"] == 0.0

    _Clock.current = datetime(2026, 6, 1, 15, 5, tzinfo=timezone.utc)
    energy.data = {"grid_power": 4.2}
    data = asyncio.run(demand._async_update_data())
    assert data["in_peak_period"] is True
    assert data["peak_demand_kw"] == 4.2
    assert data["estimated_cost"] == pytest.approx(37.8)

    _Clock.current = datetime(2026, 6, 1, 21, 0, tzinfo=timezone.utc)
    energy.data = {"grid_power": 10.99}
    data = asyncio.run(demand._async_update_data())
    assert data["in_peak_period"] is False
    assert data["grid_import_power_kw"] == 10.99
    assert data["peak_demand_kw"] == 4.2
    assert data["estimated_cost"] == pytest.approx(37.8)


def test_peak_demand_survives_same_cycle_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
):
    coordinator_module = _coordinator_module(monkeypatch)
    _Clock.current = datetime(2026, 8, 21, 17, 0, tzinfo=timezone.utc)
    energy = SimpleNamespace(data={"grid_power": 7.25})
    first = coordinator_module.DemandChargeCoordinator(
        hass=SimpleNamespace(),
        energy_coordinator=energy,
        enabled=True,
        rate=10.0,
        start_time="15:00",
        end_time="21:00",
        days="All Days",
        billing_day=1,
        entry_id="entry-1",
    )
    assert asyncio.run(first._async_update_data())["peak_demand_kw"] == 7.25

    energy.data = {"grid_power": 2.0}
    reconstructed = coordinator_module.DemandChargeCoordinator(
        hass=SimpleNamespace(),
        energy_coordinator=energy,
        enabled=True,
        rate=10.0,
        start_time="15:00",
        end_time="21:00",
        days="All Days",
        billing_day=1,
        entry_id="entry-1",
    )
    asyncio.run(reconstructed.async_load())
    assert asyncio.run(reconstructed._async_update_data())["peak_demand_kw"] == 7.25


def test_peak_demand_does_not_cross_billing_cycle(
    monkeypatch: pytest.MonkeyPatch,
):
    coordinator_module = _coordinator_module(monkeypatch)
    _Clock.current = datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)
    energy = SimpleNamespace(data={"grid_power": 7.25})
    first = coordinator_module.DemandChargeCoordinator(
        SimpleNamespace(), energy, True, 10.0, "15:00", "21:00", "All Days", 1,
        entry_id="entry-2",
    )
    asyncio.run(first._async_update_data())

    _Clock.current = datetime(2026, 9, 1, 17, 0, tzinfo=timezone.utc)
    energy.data = {"grid_power": 2.0}
    reconstructed = coordinator_module.DemandChargeCoordinator(
        SimpleNamespace(), energy, True, 10.0, "15:00", "21:00", "All Days", 1,
        entry_id="entry-2",
    )
    asyncio.run(reconstructed.async_load())
    assert asyncio.run(reconstructed._async_update_data())["peak_demand_kw"] == 2.0
