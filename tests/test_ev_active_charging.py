"""Tests for physical EV charging detection fallbacks."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace


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


VIN = "LRWYHCEK3PC907290"


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
