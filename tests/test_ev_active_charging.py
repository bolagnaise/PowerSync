"""Tests for physical EV charging detection fallbacks."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))

_ha_root = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
_ha_core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
_ha_util = sys.modules.setdefault("homeassistant.util", types.ModuleType("homeassistant.util"))
_ha_dt = sys.modules.setdefault("homeassistant.util.dt", types.ModuleType("homeassistant.util.dt"))
_ha_helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
_ha_er = sys.modules.setdefault(
    "homeassistant.helpers.entity_registry",
    types.ModuleType("homeassistant.helpers.entity_registry"),
)
_ha_dr = sys.modules.setdefault(
    "homeassistant.helpers.device_registry",
    types.ModuleType("homeassistant.helpers.device_registry"),
)

_ha_dt.now = getattr(_ha_dt, "now", lambda *args, **kwargs: None)
_ha_core.HomeAssistant = type("HomeAssistant", (), {})
_ha_er.async_get = lambda hass: hass.entity_registry
_ha_dr.async_get = lambda hass: hass.device_registry
_ha_helpers.entity_registry = _ha_er
_ha_helpers.device_registry = _ha_dr
_ha_util.dt = _ha_dt
_ha_root.helpers = _ha_helpers
_ha_root.util = _ha_util

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_optimization = types.ModuleType("power_sync.optimization")
_optimization.__path__ = [str(ROOT / "optimization")]
sys.modules["power_sync.optimization"] = _optimization

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

if not hasattr(sys.modules.get("power_sync.const"), "TESLA_INTEGRATIONS"):
    sys.modules.pop("power_sync.const", None)

ev_planner = importlib.import_module("power_sync.automations.ev_charging_planner")


VIN = "5YJTEST0000000001"


class _State:
    def __init__(self, entity_id: str, state: str, attributes: dict | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}


class _States:
    def __init__(self, states: list[_State]) -> None:
        self._states = {state.entity_id: state for state in states}

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_all(self, domain: str | None = None):
        if domain is None:
            return list(self._states.values())
        return [
            state for entity_id, state in self._states.items()
            if entity_id.startswith(f"{domain}.")
        ]


class _Entry:
    entry_id = "entry-1"
    data = {}
    options = {}


class _Hass:
    def __init__(
        self,
        states: list[_State],
        registry_entities: dict[str, object] | None = None,
        devices: dict[str, object] | None = None,
    ) -> None:
        self.states = _States(states)
        self.entity_registry = SimpleNamespace(entities=registry_entities or {})
        self.device_registry = SimpleNamespace(devices=devices or {})


def _install_registry_stubs() -> None:
    _ha_er.async_get = lambda hass: hass.entity_registry
    _ha_dr.async_get = lambda hass: hass.device_registry


def test_active_charging_inferred_from_teslemetry_bt_power():
    _install_registry_stubs()
    hass = _Hass([
        _State(f"sensor.{VIN}_charging_state", "Stopped"),
        _State(f"switch.{VIN}_charge", "off"),
        _State(f"sensor.{VIN}_charger_power", "7.2", {"unit_of_measurement": "kW"}),
    ])

    assert asyncio.run(
        ev_planner.is_ev_actively_charging(hass, _Entry(), vehicle_vin=VIN)
    ) is True


def _both_entry(prefixes: str = "ble_a,ble_b", mapping: str = "") -> _Entry:
    entry = _Entry()
    entry.options = {
        "ev_provider": "both",
        "tesla_ble_entity_prefix": prefixes,
        "tesla_ble_vehicle_mapping": mapping,
    }
    return entry


def _fleet_devices(*vins: str) -> dict[str, object]:
    return {
        f"device-{index}": SimpleNamespace(
            id=f"device-{index}",
            name=f"Tesla {index}",
            identifiers={("tesla_fleet", vin)},
        )
        for index, vin in enumerate(vins)
    }


def test_active_charging_real_vin_uses_only_its_paired_ble_prefix():
    _install_registry_stubs()
    vin_a = "5YJTEST0000000001"
    vin_b = "5YJTEST0000000002"
    hass = _Hass(
        [
            _State("switch.ble_a_charger", "off"),
            _State("switch.ble_b_charger", "on"),
        ],
        devices=_fleet_devices(vin_a, vin_b),
    )

    assert asyncio.run(
        ev_planner.is_ev_actively_charging(
            hass,
            _both_entry(
                "ble_b,ble_a",
                f"{vin_a}=ble_a,{vin_b}=ble_b",
            ),
            vehicle_vin=vin_a,
        )
    ) is False

    hass.states._states["switch.ble_a_charger"] = _State(
        "switch.ble_a_charger", "on"
    )
    assert asyncio.run(
        ev_planner.is_ev_actively_charging(
            hass,
            _both_entry(
                "ble_b,ble_a",
                f"{vin_a}=ble_a,{vin_b}=ble_b",
            ),
            vehicle_vin=vin_a,
        )
    ) is True


def test_active_charging_fleet_only_ignores_stale_ble_and_unknown_vin():
    _install_registry_stubs()
    vin_a = "5YJTEST0000000001"
    unknown_vin = "5YJTEST0000000099"
    hass = _Hass(
        [_State("switch.ble_a_charger", "on")],
        devices=_fleet_devices(vin_a),
    )
    fleet_entry = _Entry()
    fleet_entry.options = {
        "ev_provider": "fleet_api",
        "tesla_ble_entity_prefix": "ble_a",
    }

    assert asyncio.run(
        ev_planner.is_ev_actively_charging(hass, fleet_entry, vehicle_vin=vin_a)
    ) is False
    assert asyncio.run(
        ev_planner.is_ev_actively_charging(hass, _both_entry(), vehicle_vin=unknown_vin)
    ) is False


def test_active_charging_explicit_ble_vin_checks_exact_prefix():
    _install_registry_stubs()
    hass = _Hass(
        [
            _State("switch.ble_a_charger", "on"),
            _State("switch.ble_b_charger", "off"),
        ]
    )

    assert asyncio.run(
        ev_planner.is_ev_actively_charging(
            hass,
            _both_entry(),
            vehicle_vin="ble_ble_b",
        )
    ) is False


def test_discovery_hides_only_explicitly_paired_ble_duplicate():
    _install_registry_stubs()
    vin = "5YJTEST0000000001"
    hass = _Hass(
        [
            _State("binary_sensor.bridge_alpha_status", "on"),
            _State("binary_sensor.bridge_beta_status", "on"),
        ],
        devices=_fleet_devices(vin),
    )
    entry = _both_entry(
        "bridge_alpha,bridge_beta",
        f"{vin}=bridge_beta",
    )

    vehicles = asyncio.run(ev_planner.discover_all_tesla_vehicles(hass, entry))

    assert [vehicle["vin"] for vehicle in vehicles] == [vin, "ble_bridge_alpha"]


def test_ble_only_discovery_ignores_lingering_fleet_devices():
    _install_registry_stubs()
    vin_a = "5YJTEST0000000001"
    vin_b = "5YJTEST0000000002"
    hass = _Hass(
        [
            _State("binary_sensor.tesla_yf88_status", "on"),
            _State("binary_sensor.tesla_flinn_status", "on"),
        ],
        devices=_fleet_devices(vin_a, vin_b),
    )
    entry = _Entry()
    entry.options = {
        "ev_provider": "tesla_ble",
        "tesla_ble_entity_prefix": "tesla_yf88,tesla_flinn",
    }

    vehicles = asyncio.run(ev_planner.discover_all_tesla_vehicles(hass, entry))

    assert [vehicle["vin"] for vehicle in vehicles] == [
        "ble_tesla_yf88",
        "ble_tesla_flinn",
    ]
    assert {vehicle["source"] for vehicle in vehicles} == {"tesla_ble"}


def test_discovery_empty_prefix_uses_default_bridge():
    _install_registry_stubs()
    hass = _Hass([_State("binary_sensor.tesla_ble_status", "on")])

    vehicles = asyncio.run(
        ev_planner.discover_all_tesla_vehicles(hass, _both_entry(""))
    )

    assert [vehicle["vin"] for vehicle in vehicles] == ["ble_tesla_ble"]


def test_discovery_pairs_unambiguous_autodetected_bridge_to_single_fleet_vin():
    _install_registry_stubs()
    vin = "5YJTEST0000000001"
    hass = _Hass(
        [
            _State("sensor.garage_ble_charging_state", "Stopped"),
            _State("binary_sensor.garage_ble_ble_status", "on"),
        ],
        devices=_fleet_devices(vin),
    )

    vehicles = asyncio.run(
        ev_planner.discover_all_tesla_vehicles(
            hass,
            _both_entry("tesla_ble"),
        )
    )

    assert [vehicle["vin"] for vehicle in vehicles] == [vin]


def test_default_tesla_start_coalesces_paired_fleet_and_ble(monkeypatch):
    vin = "5YJTEST0000000001"
    ble_vin = "ble_ble_a"

    async def discovered(*_args, **_kwargs):
        return [
            {"vin": vin, "source": "fleet_api"},
            {"vin": ble_vin, "source": "tesla_ble"},
        ]

    async def plugged(_hass, _entry, vehicle_vin=None):
        return vehicle_vin in {vin, ble_vin}

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", discovered)
    monkeypatch.setattr(ev_planner, "get_ev_location", lambda *_args, **_kwargs: _async_home())
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged)
    assert asyncio.run(
        ev_planner._resolve_unspecified_tesla_start_vin(
            _Hass([]),
            _both_entry("ble_a"),
            None,
        )
    ) == vin


def test_default_tesla_start_remains_ambiguous_for_two_physical_fleet_cars(monkeypatch):
    vin_a = "5YJTEST0000000001"
    vin_b = "5YJTEST0000000002"

    async def discovered(*_args, **_kwargs):
        return [
            {"vin": vin_a, "source": "fleet_api"},
            {"vin": "ble_ble_a", "source": "tesla_ble"},
            {"vin": vin_b, "source": "fleet_api"},
            {"vin": "ble_ble_b", "source": "tesla_ble"},
        ]

    async def plugged(_hass, _entry, vehicle_vin=None):
        return vehicle_vin in {vin_a, vin_b, "ble_ble_a", "ble_ble_b"}

    monkeypatch.setattr(ev_planner, "discover_all_tesla_vehicles", discovered)
    monkeypatch.setattr(ev_planner, "get_ev_location", lambda *_args, **_kwargs: _async_home())
    monkeypatch.setattr(ev_planner, "is_ev_plugged_in", plugged)
    actions_stub = types.ModuleType("power_sync.automations.actions")
    actions_stub._resolve_ble_prefix_for_vehicle = lambda _hass, _entry, vin: {
        vin_a: "ble_a",
        vin_b: "ble_b",
    }.get(vin, "")
    monkeypatch.setitem(sys.modules, "power_sync.automations.actions", actions_stub)

    assert asyncio.run(
        ev_planner._resolve_unspecified_tesla_start_vin(
            _Hass([]),
            _both_entry(),
            None,
        )
    ) is None


def test_coordinated_default_tesla_start_fails_closed_before_command(monkeypatch):
    start_call = []

    async def unresolved(*_args, **_kwargs):
        return None

    async def should_not_start(*_args, **_kwargs):
        start_call.append(True)
        return True

    monkeypatch.setattr(ev_planner, "_resolve_unspecified_tesla_start_vin", unresolved)
    monkeypatch.setattr(ev_planner, "_action_start_ev_charging_dynamic", should_not_start, raising=False)

    result = asyncio.run(
        ev_planner._start_coordinated_charging(
            _Hass([]),
            "power_sync",
            _both_entry(),
            owner_mode="scheduled",
            reason="ambiguous default",
        )
    )

    assert result is False
    assert start_call == []


async def _async_home() -> str:
    return "home"


def test_active_charging_inferred_from_fleet_device_power():
    _install_registry_stubs()
    entity_id = "sensor.model_3_charge_power"
    device_id = "device-1"
    hass = _Hass(
        [_State(entity_id, "6800", {"unit_of_measurement": "W"})],
        {
            entity_id: SimpleNamespace(entity_id=entity_id, device_id=device_id),
        },
        {
            device_id: SimpleNamespace(
                id=device_id,
                name="Model 3",
                identifiers={("tessie", VIN)},
            ),
        },
    )

    assert asyncio.run(
        ev_planner.is_ev_actively_charging(hass, _Entry(), vehicle_vin=VIN)
    ) is True
