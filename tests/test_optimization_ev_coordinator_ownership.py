"""Tests for the legacy optimizer EV coordinator ownership guard."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


def _install_ha_stubs() -> None:
    ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    ha_config_entries = sys.modules.setdefault(
        "homeassistant.config_entries", types.ModuleType("homeassistant.config_entries")
    )
    ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    ha_helpers = sys.modules.setdefault(
        "homeassistant.helpers", types.ModuleType("homeassistant.helpers")
    )
    ha_er = sys.modules.setdefault(
        "homeassistant.helpers.entity_registry",
        types.ModuleType("homeassistant.helpers.entity_registry"),
    )
    ha_dr = sys.modules.setdefault(
        "homeassistant.helpers.device_registry",
        types.ModuleType("homeassistant.helpers.device_registry"),
    )
    ha_event = sys.modules.setdefault(
        "homeassistant.helpers.event", types.ModuleType("homeassistant.helpers.event")
    )
    ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
    ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
    ha_er.async_get = lambda hass: getattr(hass, "entity_registry", SimpleNamespace(entities={}))
    ha_dr.async_get = lambda hass: SimpleNamespace(devices={})
    ha_event.async_track_time_interval = lambda *args, **kwargs: (lambda: None)
    ha_event.async_track_point_in_time = lambda *args, **kwargs: (lambda: None)
    ha_dt.now = lambda *args, **kwargs: datetime.now(timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime.now(timezone.utc)
    ha_helpers.entity_registry = ha_er
    ha_helpers.device_registry = ha_dr
    ha_helpers.event = ha_event
    ha_root.helpers = ha_helpers
    ha_util.dt = ha_dt
    ha_root.util = ha_util


_install_ha_stubs()

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

ev_ownership = importlib.import_module("power_sync.automations.ev_ownership")
ev_coordinator = importlib.import_module("power_sync.optimization.ev_coordinator")


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _States:
    def get(self, entity_id: str):
        return None


class _Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    async def async_call(self, domain: str, service: str, data: dict, **kwargs):
        self.calls.append((domain, service, data))


class _ConfigEntries:
    def __init__(self, entries=None) -> None:
        self._entries = entries or []

    def async_entries(self, domain: str):
        return self._entries if domain == "power_sync" else []


class _Hass:
    def __init__(self, entries=None) -> None:
        self.data = {"power_sync": {"entry-1": {}}}
        self.states = _States()
        self.services = _Services()
        self.config_entries = _ConfigEntries(entries)


def test_optimizer_ev_start_is_blocked_by_existing_owner():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)

    ev_ownership.claim_ev_ownership(
        hass,
        entry,
        ev_ownership.DEFAULT_VEHICLE_ID,
        owner_mode="solar_surplus",
    )

    result = asyncio.run(coordinator._start_charging(config))

    assert result is False
    assert hass.services.calls == []
    last_command = ev_ownership.get_ev_last_commands(hass, entry)["switch.garage_ev"]
    assert last_command["command"] == "start_ev_coordinator"
    assert last_command["success"] is False
    assert "solar_surplus" in last_command["reason"]


def test_optimizer_ev_start_claims_ownership_after_physical_start():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)

    result = asyncio.run(coordinator._start_charging(config))

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_on", {"entity_id": "switch.garage_ev"})
    ]
    lease = ev_ownership.get_ev_ownerships(hass, entry)["switch.garage_ev"]
    assert lease["owner_mode"] == "ev_coordinator"
    assert lease["last_command"]["command"] == "start_ev_coordinator"


def test_optimizer_ev_stop_refuses_unowned_charging():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)

    result = asyncio.run(coordinator._stop_charging(config))

    assert result is False
    assert hass.services.calls == []
    last_command = ev_ownership.get_ev_last_commands(hass, entry)["switch.garage_ev"]
    assert last_command["command"] == "stop_ev_coordinator"
    assert last_command["success"] is False


def test_optimizer_ev_stop_releases_owned_loadpoint():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)
    ev_ownership.claim_ev_ownership(
        hass,
        entry,
        "switch.garage_ev",
        owner_mode="ev_coordinator",
    )

    result = asyncio.run(coordinator._stop_charging(config, reason="target reached"))

    assert result is True
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.garage_ev"})
    ]
    assert ev_ownership.get_ev_ownerships(hass, entry) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, entry)["switch.garage_ev"]
    assert last_command["command"] == "stop_ev_coordinator"
    assert last_command["success"] is True
    assert last_command["reason"] == "target reached"


def test_optimizer_wallbox_routes_through_shared_native_adapter():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="wallbox.garage", name="Garage Wallbox")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)

    result = asyncio.run(coordinator._start_charging(config, power_w=3680))

    assert result is True
    assert hass.services.calls == [
        ("wallbox", "set_charging_current", {"entity_id": "wallbox.garage", "charging_current": 16}),
        ("wallbox", "start_charging", {"entity_id": "wallbox.garage"}),
    ]
    lease = ev_ownership.get_ev_ownerships(hass, entry)["wallbox.garage"]
    assert lease["owner_mode"] == "ev_coordinator"

    hass.services.calls.clear()

    result = asyncio.run(coordinator._stop_charging(config, reason="window ended"))

    assert result is True
    assert hass.services.calls == [
        ("wallbox", "stop_charging", {"entity_id": "wallbox.garage"})
    ]
    assert ev_ownership.get_ev_ownerships(hass, entry) == {}


def test_optimizer_easee_routes_through_shared_native_adapter():
    hass = _Hass()
    entry = _Entry()
    config = ev_coordinator.EVConfig(entity_id="easee.garage", name="Garage Easee")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=entry)

    result = asyncio.run(coordinator._start_charging(config, power_w=2300))

    assert result is True
    assert hass.services.calls == [
        ("easee", "set_charger_dynamic_limit", {"entity_id": "easee.garage", "current": 10}),
        ("easee", "start_charging", {"entity_id": "easee.garage"}),
    ]


def test_optimizer_native_zaptec_routes_through_shared_native_adapter():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"zaptec_installation_id": "installation-device"},
    )
    hass = _Hass(entries=[entry])
    config = ev_coordinator.EVConfig(entity_id="zaptec.garage", name="Garage Zaptec")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())

    result = asyncio.run(coordinator._start_charging(config, power_w=2300))

    assert result is True
    assert hass.services.calls == [
        ("zaptec", "limit_current", {"device_id": "installation-device", "available_current": 10}),
        ("zaptec", "resume_charging", {"entity_id": "zaptec.garage"}),
    ]


def test_optimizer_zaptec_uses_existing_client_without_password():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
        },
    )
    hass = _Hass(entries=[entry])
    client = SimpleNamespace(
        resume_charging=AsyncMock(return_value=True),
        set_installation_current=AsyncMock(return_value=True),
    )
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "zaptec_cached_state": {"cable_locked": True},
        }
    )
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())

    result = asyncio.run(coordinator._start_charging(config))

    assert result is True
    client.resume_charging.assert_awaited_once_with("charger-1")
    lease = ev_ownership.get_ev_ownerships(hass, _Entry())["switch.garage_ev"]
    assert lease["owner_mode"] == "ev_coordinator"


def test_optimizer_zaptec_waiting_requires_installation_current():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
        },
    )
    hass = _Hass(entries=[entry])
    client = SimpleNamespace(
        resume_charging=AsyncMock(return_value=True),
        set_installation_current=AsyncMock(return_value=True),
    )
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "zaptec_cached_state": {"charger_operation_mode": "connected_waiting"},
        }
    )
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())

    result = asyncio.run(coordinator._start_charging(config))

    assert result is False
    client.resume_charging.assert_not_awaited()
    client.set_installation_current.assert_not_awaited()
    assert ev_ownership.get_ev_ownerships(hass, _Entry()) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, _Entry())["switch.garage_ev"]
    assert last_command["command"] == "start_ev_coordinator"
    assert last_command["success"] is False
    assert "installation current" in last_command["reason"]


def test_optimizer_zaptec_waiting_sets_current_and_claims():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
            "zaptec_installation_id_cloud": "installation-1",
        },
    )
    hass = _Hass(entries=[entry])
    client = SimpleNamespace(
        resume_charging=AsyncMock(return_value=True),
        set_installation_current=AsyncMock(return_value=True),
    )
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "zaptec_cached_state": {"charger_operation_mode": "connected_waiting"},
        }
    )
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())

    result = asyncio.run(coordinator._start_charging(config, power_w=3680))

    assert result is True
    client.set_installation_current.assert_awaited_once_with("installation-1", 16)
    client.resume_charging.assert_not_awaited()
    lease = ev_ownership.get_ev_ownerships(hass, _Entry())["switch.garage_ev"]
    assert lease["owner_mode"] == "ev_coordinator"


def test_optimizer_zaptec_already_charging_updates_current_without_resume():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
            "zaptec_installation_id_cloud": "installation-1",
        },
    )
    hass = _Hass(entries=[entry])
    client = SimpleNamespace(
        resume_charging=AsyncMock(return_value=True),
        set_installation_current=AsyncMock(return_value=True),
    )
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "zaptec_cached_state": {"charger_operation_mode": "charging"},
        }
    )
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())

    result = asyncio.run(coordinator._start_charging(config, power_w=2300))

    assert result is True
    client.set_installation_current.assert_awaited_once_with("installation-1", 10)
    client.resume_charging.assert_not_awaited()


def test_optimizer_zaptec_idle_stop_releases_without_stop_command():
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "zaptec_standalone_enabled": True,
            "zaptec_username": "user@example.com",
            "zaptec_charger_id": "charger-1",
        },
    )
    hass = _Hass(entries=[entry])
    client = SimpleNamespace(stop_charging=AsyncMock(return_value=True))
    hass.data["power_sync"]["entry-1"].update(
        {
            "zaptec_client": client,
            "zaptec_cached_state": {"charger_operation_mode": "connected_waiting"},
        }
    )
    config = ev_coordinator.EVConfig(entity_id="switch.garage_ev", name="Garage EV")
    coordinator = ev_coordinator.EVCoordinator(hass, [config], config_entry=_Entry())
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        "switch.garage_ev",
        owner_mode="ev_coordinator",
    )

    result = asyncio.run(coordinator._stop_charging(config, reason="waiting"))

    assert result is True
    client.stop_charging.assert_not_awaited()
    assert ev_ownership.get_ev_ownerships(hass, _Entry()) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, _Entry())["switch.garage_ev"]
    assert last_command["command"] == "release"
    assert last_command["reason"] == "waiting"


def test_optimizer_vehicle_following_tesla_away_releases_without_stop(monkeypatch):
    from homeassistant.helpers import entity_registry as er, device_registry as dr

    vehicle_vin = "LRW3F7FS1NC484342"
    device = SimpleNamespace(
        id="tesla-device",
        identifiers={("tesla_fleet", vehicle_vin)},
    )
    hass = _Hass()
    hass.entity_registry = SimpleNamespace(
        entities={
            "switch.keksla_charge": SimpleNamespace(
                entity_id="switch.keksla_charge",
                device_id=device.id,
            )
        }
    )
    hass.device_registry = SimpleNamespace(devices={device.id: device})
    monkeypatch.setattr(er, "async_get", lambda _hass: hass.entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: hass.device_registry)

    planner = types.ModuleType("power_sync.automations.ev_charging_planner")

    def iter_tesla_devices(device_registry):
        yield device_registry.devices[device.id], vehicle_vin

    async def away_location(*args, **kwargs):
        return "remote_charger"

    planner._iter_tesla_vehicle_devices = iter_tesla_devices
    planner.get_ev_location = away_location
    monkeypatch.setitem(
        sys.modules,
        "power_sync.automations.ev_charging_planner",
        planner,
    )

    config = ev_coordinator.EVConfig(
        entity_id="switch.keksla_charge",
        name="Keksla",
    )
    coordinator = ev_coordinator.EVCoordinator(
        hass,
        [config],
        config_entry=_Entry(),
    )
    ev_ownership.claim_ev_ownership(
        hass,
        _Entry(),
        config.entity_id,
        owner_mode="ev_coordinator",
    )

    result = asyncio.run(
        coordinator._stop_charging(config, reason="waiting for cheap rate")
    )

    assert result is True
    assert hass.services.calls == []
    assert ev_ownership.get_ev_ownerships(hass, _Entry()) == {}
    last_command = ev_ownership.get_ev_last_commands(hass, _Entry())[config.entity_id]
    assert last_command["command"] == "release"
    assert "away" in last_command["reason"]
