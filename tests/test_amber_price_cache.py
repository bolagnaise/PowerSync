"""Tests for Amber forecast cache behavior."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


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
tariff_converter = importlib.import_module("power_sync.tariff_converter")


class _FakeHass:
    def __init__(self, data=None, states=None) -> None:
        self.data = data or {}
        self.session = object()
        self.states = _FakeStates(states or [])
        # The energy accumulator resolves the entry's plan for daily cost tracking.
        self.config_entries = SimpleNamespace(async_get_entry=lambda _entry_id: None)


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


class _SequenceSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def get(self, url, *args, **kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


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


def _current_interval(nem_time: str, buy: float, sell: float, duration: int = 5):
    return [
        {
            "channelType": "general",
            "type": "CurrentInterval",
            "nemTime": nem_time,
            "perKwh": buy,
            "duration": duration,
        },
        {
            "channelType": "feedIn",
            "type": "CurrentInterval",
            "nemTime": nem_time,
            "perKwh": sell,
            "duration": duration,
        },
    ]


def _full_30min_forecast():
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    forecast = []
    for slot in range(48):
        interval_end = start + timedelta(minutes=(slot + 1) * 30)
        for channel, price in (("general", 26.0), ("feedIn", -13.0)):
            forecast.append({
                "channelType": channel,
                "type": "ForecastInterval",
                "nemTime": interval_end.isoformat(),
                "perKwh": price,
                "duration": 30,
                "advancedPrice": {"predicted": price},
            })
    return forecast


def test_amber_current_interval_uses_nem_offset_during_victorian_daylight_saving():
    current = _current_interval("2026-10-20T14:30:00+10:00", 1.77, -4.08, 30)
    now = datetime.fromisoformat("2026-10-20T15:17:00+11:00")
    selected = tariff_converter.extract_current_price_interval(current, now=now)
    assert selected["general"]["perKwh"] == 1.77
    assert selected["feedIn"]["perKwh"] == -4.08
    # The same payload stops being current at its actual end, regardless of DST.
    assert tariff_converter.extract_current_price_interval(
        current, now=datetime.fromisoformat("2026-10-20T15:30:00+11:00")
    ) is None


@pytest.mark.parametrize("duration", [5, 15, 30])
def test_amber_tou_uses_refreshed_current_after_cached_boundary(monkeypatch, duration):
    """A cached five-minute interval must not become the new TOU period."""
    clock = {"now": datetime(2026, 5, 1, 11, 29, 50, tzinfo=timezone.utc)}
    old_current = _current_interval("2026-05-01T11:30:00+00:00", 62.0, -47.0, duration)
    fresh_end = datetime(2026, 5, 1, 11, 30, tzinfo=timezone.utc) + timedelta(minutes=duration)
    fresh_current = _current_interval(fresh_end.isoformat(), 25.6, -13.2, duration)
    refresh_count = {"current": 0}

    async def fake_fetch(session, url, headers, **kwargs):
        params = kwargs.get("params")
        if params is None:
            refresh_count["current"] += 1
            return old_current if refresh_count["current"] == 1 else fresh_current
        if params == {"resolution": 5}:
            return old_current
        if params == {"next": 288, "resolution": 30}:
            return _forecast_30min()
        raise AssertionError(f"unexpected Amber request params: {params}")

    monkeypatch.setattr(coordinator.dt_util, "utcnow", lambda: clock["now"])
    monkeypatch.setattr(tariff_converter.dt_util, "now", lambda: clock["now"])
    monkeypatch.setattr(coordinator, "_fetch_with_retry", fake_fetch)

    amber = coordinator.AmberPriceCoordinator(_FakeHass(), "token", site_id="site-1")
    asyncio.run(amber._async_update_data())

    clock["now"] = datetime(2026, 5, 1, 11, 31, tzinfo=timezone.utc)
    result = asyncio.run(amber._async_update_data())

    assert result["forecast_5min"] == old_current
    assert tariff_converter.extract_current_price_interval(
        result["forecast_5min"], now=clock["now"]
    ) is None
    selected = tariff_converter.extract_current_price_interval(
        result["current"], now=clock["now"]
    )
    assert selected["general"]["perKwh"] == 25.6
    assert selected["feedIn"]["perKwh"] == -13.2

    real_datetime = tariff_converter.datetime

    class FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            current = real_datetime(2026, 5, 1, 11, 31, tzinfo=timezone.utc)
            return current.astimezone(tz) if tz else current

    monkeypatch.setattr(tariff_converter, "datetime", FixedDatetime)
    tariff = tariff_converter.convert_amber_to_tesla_tariff(
        _full_30min_forecast(),
        tesla_energy_site_id="none",
        powerwall_timezone="UTC",
        current_actual_interval=selected,
        electricity_provider="amber",
    )
    buy_rates = tariff["energy_charges"]["Summer"]["rates"]
    sell_rates = tariff["sell_tariff"]["energy_charges"]["Summer"]["rates"]
    assert buy_rates["PERIOD_11_30"] == 0.256
    assert sell_rates["PERIOD_11_30"] == 0.132
    assert buy_rates["PERIOD_11_30"] != 0.62
    assert sell_rates["PERIOD_11_30"] != 0.47


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


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"token expired (401)"}',
        '{"error":"token_revoked","action":"reauth_required"}',
    ],
)
def test_fetch_with_retry_treats_fleet_token_401_as_update_failure(body):
    session = _FakeSession(_FakeResponse(401, body))

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
        assert body in str(err)
    else:
        raise AssertionError("Expected UpdateFailed")


def test_fetch_with_retry_confirms_powersync_bearer_before_reauth():
    session = _SequenceSession(
        [
            _FakeResponse(401, '{"error":"invalid_token"}'),
            _FakeResponse(200, payload={"authenticated": True}),
        ]
    )

    try:
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                f"{coordinator.POWERSYNC_API_BASE_URL}/api/1/products",
                {"Authorization": "Bearer psync_test_token"},
                max_retries=1,
            )
        )
    except coordinator.UpdateFailed as err:
        assert "bearer remains valid" in str(err)
        assert session.urls[-1] == coordinator.POWERSYNC_AUTH_ME_URL
    else:
        raise AssertionError("Expected retryable UpdateFailed")


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"token_revoked","action":"reauth_required"}',
        '{"error":"token_revoked"}',
        '{"error":"tesla_auth_expired","action":"reauth_required"}',
    ],
)
def test_fetch_with_retry_reauths_revoked_tesla_connection_with_valid_powersync_bearer(body):
    session = _SequenceSession(
        [
            _FakeResponse(401, body),
            _FakeResponse(200, payload={"authenticated": True}),
        ]
    )
    url = f"{coordinator.POWERSYNC_API_BASE_URL}/api/1/energy_sites/site-1/live_status"

    with pytest.raises(coordinator.ConfigEntryAuthFailed, match="Tesla"):
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                url,
                {"Authorization": "Bearer psync_test_token"},
                max_retries=1,
            )
        )

    # Explicit upstream revocation ends the retry loop immediately. The
    # PowerSync session can remain valid independently of Tesla authorization.
    assert session.urls == [url]


@pytest.mark.parametrize(
    "body",
    [
        '{"error":"token_refresh_failed"}',
        '<html>Unauthorized</html>',
        'null',
        '["token_revoked"]',
    ],
)
@pytest.mark.parametrize("bearer_status", [200, 503])
def test_fetch_with_retry_keeps_unspecified_proxy_401_retryable(body, bearer_status):
    session = _SequenceSession(
        [_FakeResponse(401, body), _FakeResponse(bearer_status)]
    )

    with pytest.raises(coordinator.UpdateFailed, match="Transient PowerSync"):
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                f"{coordinator.POWERSYNC_API_BASE_URL}/api/1/products",
                {"Authorization": "Bearer psync_test_token"},
                max_retries=1,
            )
        )

    assert session.urls[-1] == coordinator.POWERSYNC_AUTH_ME_URL


def test_fetch_with_retry_reauths_when_powersync_bearer_is_invalid():
    session = _SequenceSession(
        [
            _FakeResponse(401, '{"error":"invalid_token"}'),
            _FakeResponse(401, '{"error":"invalid_token"}'),
        ]
    )

    try:
        asyncio.run(
            coordinator._fetch_with_retry(
                session,
                f"{coordinator.POWERSYNC_API_BASE_URL}/api/1/products",
                {"Authorization": "Bearer psync_test_token"},
                max_retries=1,
            )
        )
    except coordinator.ConfigEntryAuthFailed as err:
        assert "invalid_token" in str(err)
        assert session.urls[-1] == coordinator.POWERSYNC_AUTH_ME_URL
    else:
        raise AssertionError("Expected ConfigEntryAuthFailed")


@pytest.mark.parametrize("revoked_tesla_auth", [False, True])
def test_tesla_auth_expiry_sends_one_actionable_mobile_alert(monkeypatch, revoked_tesla_auth):
    # Other coordinator fixtures replace HA's shared-session provider during
    # collection. Pin the live provider used by the controlled-session helper,
    # so this test exercises its own rejected response in any file order.
    monkeypatch.setattr(
        sys.modules["homeassistant.helpers.aiohttp_client"],
        "async_get_clientsession",
        lambda hass: hass.session,
    )
    hass = _FakeHass()
    if revoked_tesla_auth:
        hass.session = _FakeSession(
            _FakeResponse(401, '{"error":"token_revoked","action":"reauth_required"}')
        )
    tesla = coordinator.TeslaEnergyCoordinator(
        hass,
        "site-1",
        "psync_test_token",
        api_provider=coordinator.TESLA_PROVIDER_POWERSYNC,
    )
    tesla._energy_acc._last_update = datetime.now(timezone.utc)
    tesla._lifetime_totals_restored = True

    async def rejected_bearer(*args, **kwargs):
        raise coordinator.ConfigEntryAuthFailed("token rejected")

    alerts = []

    async def capture_push(hass_arg, title, message):
        alerts.append((hass_arg, title, message))

    automations_package = types.ModuleType("power_sync.automations")
    automations_package.__path__ = []
    actions_module = types.ModuleType("power_sync.automations.actions")
    actions_module._send_expo_push = capture_push
    monkeypatch.setitem(sys.modules, "power_sync.automations", automations_package)
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_module)
    if not revoked_tesla_auth:
        monkeypatch.setattr(coordinator, "_fetch_with_retry", rejected_bearer)

    for _ in range(2):
        try:
            asyncio.run(tesla._async_update_data())
        except coordinator.ConfigEntryAuthFailed:
            pass
        else:
            raise AssertionError("Expected ConfigEntryAuthFailed")

    assert tesla._consecutive_failures == 0
    assert tesla._outage_notified is False
    assert alerts == [
        (
            hass,
            "PowerSync Connection Expired",
            "Re-authenticate in Home Assistant Settings > Repairs. "
            "Automations and battery control may be unavailable.",
        )
    ]


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


def test_tesla_partial_live_status_missing_battery_power_fails_instead_of_publishing_zero(
    monkeypatch,
):
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )
    tesla._energy_acc._last_update = datetime.now(timezone.utc)
    tesla._lifetime_totals_restored = True

    async def partial_live_status(*args, **kwargs):
        return {
            "response": {
                "solar_power": 1300,
                "grid_power": -1100,
                "load_power": 240,
                "percentage_charged": 0,
            }
        }

    monkeypatch.setattr(coordinator, "_fetch_with_retry", partial_live_status)

    try:
        asyncio.run(tesla._async_update_data())
    except coordinator.UpdateFailed as err:
        assert "battery_power" in str(err)
    else:
        raise AssertionError("Expected partial battery telemetry to fail")


def test_tesla_explicit_zero_battery_power_remains_a_valid_measurement(monkeypatch):
    tesla = coordinator.TeslaEnergyCoordinator(
        _FakeHass(),
        "site-1",
        "token",
        entry_id="entry-1",
    )
    tesla._energy_acc._last_update = datetime.now(timezone.utc)
    tesla._lifetime_totals_restored = True
    tesla._energy_acc.update = lambda *args, **kwargs: None

    async def zero_battery_power(*args, **kwargs):
        return {
            "response": {
                "solar_power": 1300,
                "grid_power": -1100,
                "battery_power": 0,
                "load_power": 240,
                "percentage_charged": 0,
            }
        }

    monkeypatch.setattr(coordinator, "_fetch_with_retry", zero_battery_power)

    data = asyncio.run(tesla._async_update_data())

    assert data["battery_power"] == 0.0
    assert data["battery_level"] == 0.0


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
