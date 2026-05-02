"""Tests for Amber forecast cache behavior."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))

_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_core.HomeAssistant = object

_ha_exceptions = sys.modules.setdefault(
    "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
)


class _ConfigEntryAuthFailed(Exception):
    pass


_ha_exceptions.ConfigEntryAuthFailed = _ConfigEntryAuthFailed

_ha_helpers = sys.modules.setdefault(
    "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
)

_ha_update = sys.modules.setdefault(
    "homeassistant.helpers.update_coordinator",
    types.ModuleType("homeassistant.helpers.update_coordinator"),
)


class _UpdateFailed(Exception):
    pass


class _DataUpdateCoordinator:
    def __init__(self, hass, logger, name=None, update_interval=None) -> None:
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None


_ha_update.DataUpdateCoordinator = _DataUpdateCoordinator
_ha_update.UpdateFailed = _UpdateFailed

_ha_aiohttp = sys.modules.setdefault(
    "homeassistant.helpers.aiohttp_client",
    types.ModuleType("homeassistant.helpers.aiohttp_client"),
)
_ha_aiohttp.async_get_clientsession = lambda hass: hass.session

_ha_dispatcher = sys.modules.setdefault(
    "homeassistant.helpers.dispatcher",
    types.ModuleType("homeassistant.helpers.dispatcher"),
)
_ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None

_ha_storage = sys.modules.setdefault(
    "homeassistant.helpers.storage", types.ModuleType("homeassistant.helpers.storage")
)


class _Store:
    def __init__(self, *args, **kwargs) -> None:
        pass


_ha_storage.Store = _Store

_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_dt.utcnow = lambda: datetime.now(timezone.utc)
_ha_dt.now = lambda *args, **kwargs: datetime.now(timezone.utc)
_ha_util.dt = _ha_dt
_ha_root.util = _ha_util

_ha_helpers.update_coordinator = _ha_update
_ha_helpers.aiohttp_client = _ha_aiohttp
_ha_helpers.dispatcher = _ha_dispatcher
_ha_helpers.storage = _ha_storage

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

coordinator = importlib.import_module("power_sync.coordinator")


class _FakeHass:
    def __init__(self, data=None) -> None:
        self.data = data or {}
        self.session = object()


def _current_price():
    return [
        {
            "channelType": "general",
            "nemTime": "2026-05-01T00:05:00+00:00",
            "perKwh": 12,
            "duration": 5,
        }
    ]


def _forecast_5min():
    return [
        {
            "channelType": "general",
            "nemTime": "2026-05-01T00:05:00+00:00",
            "perKwh": 12,
            "duration": 5,
        }
    ]


def _forecast_30min():
    return [
        {
            "channelType": "general",
            "nemTime": "2026-05-01T00:35:00+00:00",
            "perKwh": 8,
            "duration": 30,
        }
    ]


def test_amber_extended_forecast_cache_reduces_rest_calls(monkeypatch):
    clock = {"now": datetime(2026, 5, 1, tzinfo=timezone.utc)}
    calls = []

    async def fake_fetch(session, url, headers, **kwargs):
        params = kwargs.get("params")
        calls.append(params)
        if params == {"resolution": 5}:
            return _forecast_5min()
        if params == {"next": 288, "resolution": 30}:
            return _forecast_30min()
        return _current_price()

    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(coordinator, "_fetch_with_retry", fake_fetch)

    amber = coordinator.AmberPriceCoordinator(_FakeHass(), "token", site_id="site-1")
    asyncio.run(amber._async_update_data())

    clock["now"] += timedelta(minutes=10)
    asyncio.run(amber._async_update_data())

    assert calls.count(None) == 2
    assert calls.count({"resolution": 5}) == 2
    assert calls.count({"next": 288, "resolution": 30}) == 1


def test_amber_extended_forecast_cache_survives_refresh_failure(monkeypatch):
    clock = {"now": datetime(2026, 5, 1, tzinfo=timezone.utc)}
    fail_30min = {"enabled": False}

    async def fake_fetch(session, url, headers, **kwargs):
        params = kwargs.get("params")
        if params == {"resolution": 5}:
            return _forecast_5min()
        if params == {"next": 288, "resolution": 30}:
            if fail_30min["enabled"]:
                raise coordinator.UpdateFailed("rate limited")
            return _forecast_30min()
        return _current_price()

    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(coordinator, "_fetch_with_retry", fake_fetch)

    amber = coordinator.AmberPriceCoordinator(_FakeHass(), "token", site_id="site-1")
    asyncio.run(amber._async_update_data())

    clock["now"] += timedelta(minutes=31)
    fail_30min["enabled"] = True
    result = asyncio.run(amber._async_update_data())

    assert result["forecast"] == _forecast_5min() + _forecast_30min()


def test_octopus_integration_synthesizes_export_when_integration_has_import_only():
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    end = now + timedelta(minutes=25)
    hass = _FakeHass({
        "octopus_energy": {
            "account-1": {
                "ACCOUNT": SimpleNamespace(
                    account={
                        "electricity_meter_points": [{
                            "mpan": "1234567890",
                            "meters": [{
                                "serial_number": "ABC123",
                                "is_export": False,
                            }],
                            "agreements": [{
                                "tariff_code": "E-1R-INTELLI-VAR-24-10-29-C",
                            }],
                        }]
                    }
                ),
                "ELECTRICITY_RATES_1234567890_ABC123": SimpleNamespace(
                    rates=[{
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "value_inc_vat": 24.5,
                    }]
                ),
            }
        }
    })
    octopus = coordinator.OctopusPriceCoordinator(
        hass,
        product_code="INTELLI-VAR-24-10-29",
        tariff_code="E-1R-INTELLI-VAR-24-10-29-C",
        gsp_region="C",
    )

    result = octopus._read_from_octopus_energy_integration()

    assert result is not None
    current = result["current"]
    assert next(p for p in current if p["channelType"] == "general")["perKwh"] == 24.5
    assert next(p for p in current if p["channelType"] == "feedIn")["perKwh"] == -4.1
    assert result["export_rates"][0]["channelType"] == "feedIn"
