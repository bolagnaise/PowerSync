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
    def __init__(self, data=None, states=None) -> None:
        self.data = data or {}
        self.session = object()
        self.states = _FakeStates(states or [])


class _FakeStates:
    def __init__(self, states) -> None:
        self._states = states

    def async_all(self, domain=None):
        if domain is None:
            return list(self._states)
        return [
            state for state in self._states
            if state.entity_id.split(".", 1)[0] == domain
        ]


class _FakeResponse:
    def __init__(self, status: int, text: str = "", payload=None, headers=None) -> None:
        self.status = status
        self._text = text
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self):
        return self._text

    async def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def get(self, *args, **kwargs):
        return self.response


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


def test_octopus_integration_reads_public_current_rate_entities_when_internal_data_missing():
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=5)
    end = now + timedelta(minutes=25)
    states = [
        SimpleNamespace(
            entity_id="sensor.octopus_energy_electricity_ABC123_1234567890_current_rate",
            state="0.245",
            attributes={
                "is_export": False,
                "tariff": "E-1R-INTELLI-VAR-24-10-29-C",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        ),
        SimpleNamespace(
            entity_id="sensor.octopus_energy_electricity_ABC123_1234567890_export_current_rate",
            state="0.041",
            attributes={
                "is_export": True,
                "tariff": "E-1R-OUTGOING-FIX-12M-19-05-13-C",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        ),
    ]
    hass = _FakeHass(states=states)
    octopus = coordinator.OctopusPriceCoordinator(
        hass,
        product_code="INTELLI-VAR-24-10-29",
        tariff_code="E-1R-INTELLI-VAR-24-10-29-C",
        gsp_region="C",
    )

    result = octopus._read_from_octopus_energy_integration()

    assert result is not None
    assert result["source"] == "octopus_energy_entities"
    current = result["current"]
    assert next(p for p in current if p["channelType"] == "general")["perKwh"] == 24.5
    assert next(p for p in current if p["channelType"] == "feedIn")["perKwh"] == -4.1


def test_fetch_with_retry_raises_reauth_for_direct_token_401():
    session = _FakeSession(_FakeResponse(401, '{"error":"token expired (401)"}'))

    try:
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                "https://example.test",
                {},
                max_retries=1,
            )
        )
    except coordinator.ConfigEntryAuthFailed as err:
        assert "token expired" in str(err)
    else:
        raise AssertionError("Expected ConfigEntryAuthFailed")


def test_fetch_with_retry_treats_fleet_token_401_as_update_failure():
    session = _FakeSession(_FakeResponse(401, '{"error":"token expired (401)"}'))

    try:
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                "https://example.test",
                {},
                max_retries=1,
                raise_auth_failed=False,
            )
        )
    except coordinator.UpdateFailed as err:
        assert "Authentication failed: 401" in str(err)
        assert "token expired" in str(err)
    else:
        raise AssertionError("Expected UpdateFailed")


def test_tesla_lifetime_totals_clamp_prevents_recorder_decrease():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )
    previous = {key: 0.0 for key in coordinator.LIFETIME_TOTAL_KEYS}
    previous["lifetime_solar_kwh"] = 1000.0
    previous["lifetime_grid_export_kwh"] = 604.96
    tesla._lifetime_totals = previous

    updated = dict(previous)
    updated["lifetime_solar_kwh"] = 1000.2
    updated["lifetime_grid_export_kwh"] = 604.958

    clamped = tesla._clamp_lifetime_totals(updated)

    assert clamped["lifetime_solar_kwh"] == 1000.2
    assert clamped["lifetime_grid_export_kwh"] == 604.96


def test_tesla_lifetime_totals_coerces_persisted_values():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )

    totals = tesla._coerce_lifetime_totals({
        "lifetime_grid_export_kwh": "604.96",
        "lifetime_solar_kwh": None,
        "lifetime_home_kwh": "not-a-number",
    })

    assert totals == {"lifetime_grid_export_kwh": 604.96}


def test_tesla_battery_level_preserves_last_valid_soc_when_live_status_omits_it():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )

    assert tesla._resolve_battery_level_pct({"percentage_charged": 84.3}) == 84.3
    assert tesla._resolve_battery_level_pct({"wall_connectors": []}) == 84.3


def test_tesla_battery_level_missing_without_cache_returns_none():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )

    assert tesla._resolve_battery_level_pct({"wall_connectors": []}) is None


def test_tesla_uses_powerwall_local_snapshot_when_cloud_status_is_empty():
    snap = SimpleNamespace(
        soc=76.5,
        solar_w=5200.0,
        battery_w=-3100.0,
        grid_w=400.0,
        load_w=2500.0,
        grid_status="SystemGridConnected",
        total_pack_full_wh=27000.0,
        total_pack_remaining_wh=20655.0,
    )
    hass = _FakeHass(
        data={
            "power_sync": {
                "entry-1": {
                    "powerwall_local": {
                        "coordinator": SimpleNamespace(data=snap),
                    },
                },
            },
        },
    )
    tesla = coordinator.TeslaEnergyCoordinator(
        hass,
        "site-1",
        "token",
        entry_id="entry-1",
    )

    data = tesla._local_powerwall_energy_data()

    assert data["data_source"] == "powerwall_local"
    assert data["battery_level"] == 76.5
    assert data["solar_power"] == 5.2
    assert data["battery_power"] == -3.1
    assert data["grid_power"] == 0.4
    assert data["load_power"] == 2.5
    assert data["grid_status"] == "Active"
    assert data["total_pack_energy_kwh"] == 27.0
    assert data["energy_left_kwh"] == 20.66


def test_tesla_outage_notification_waits_for_sustained_failure():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )

    for now in (100.0, 115.0, 130.0, 145.0, 160.0):
        should_notify, failure_duration = tesla._record_tesla_update_failure(now)

    assert tesla._consecutive_failures == 5
    assert tesla._failure_streak_start == 100.0
    assert failure_duration == 60.0
    assert should_notify is False
    assert tesla._outage_notified is False


def test_tesla_outage_notification_fires_after_sustained_failure():
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )

    for now in (100.0, 115.0, 130.0, 145.0, 401.0):
        should_notify, failure_duration = tesla._record_tesla_update_failure(now)

    assert tesla._consecutive_failures == 5
    assert tesla._failure_streak_start == 100.0
    assert failure_duration == 301.0
    assert should_notify is True


def test_stored_battery_health_capacity_uses_bms_current_capacity():
    hass = _FakeHass(
        data={
            "power_sync": {
                "entry-1": {
                    "battery_health": {
                        "current_capacity_wh": 43250.0,
                    },
                },
            },
        }
    )

    assert coordinator._stored_battery_health_capacity_kwh(hass, "entry-1") == 43.25
