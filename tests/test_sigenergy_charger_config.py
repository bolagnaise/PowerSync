"""Tests for Sigenergy EV charger connection resolution."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
_SENTINEL = object()


@pytest.fixture()
def config_module():
    saved_power_sync = sys.modules.get("power_sync", _SENTINEL)
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync
    try:
        yield importlib.import_module("power_sync.sigenergy_charger_config")
    finally:
        sys.modules.pop("power_sync.sigenergy_charger_config", None)
        sys.modules.pop("power_sync.const", None)
        if saved_power_sync is _SENTINEL:
            sys.modules.pop("power_sync", None)
        else:
            sys.modules["power_sync"] = saved_power_sync


def _hass_with_vehicle_config(entry_id: str, config: dict) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            "power_sync": {
                entry_id: {
                    "automation_store": SimpleNamespace(
                        _data={"vehicle_charging_configs": [config]}
                    )
                }
            }
        }
    )


def test_stored_sigenergy_host_wins_when_entry_host_is_modbus_default(config_module):
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_modbus_host": "192.168.10.90",
            "sigenergy_charger_host": "192.168.10.90",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 1,
            "sigenergy_charger_type": "evdc",
        },
    )
    hass = _hass_with_vehicle_config(
        "entry-1",
        {
            "vehicle_id": "sigenergy_charger",
            "charger_type": "sigenergy",
            "sigenergy_charger_host": "192.168.10.102",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 247,
            "sigenergy_charger_type": "evdc",
        },
    )

    config = config_module.resolve_sigenergy_charger_connection(entry, hass=hass)

    assert config["host"] == "192.168.10.102"
    assert config["slave_id"] == 247
    assert config["charger_type"] == "evdc"


def test_explicit_dedicated_entry_host_wins_over_stored_config(config_module):
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_modbus_host": "192.168.10.90",
            "sigenergy_charger_host": "192.168.10.103",
            "sigenergy_charger_port": 502,
            "sigenergy_charger_slave_id": 2,
            "sigenergy_charger_type": "evdc",
        },
    )
    hass = _hass_with_vehicle_config(
        "entry-1",
        {
            "vehicle_id": "sigenergy_charger",
            "charger_type": "sigenergy",
            "sigenergy_charger_host": "192.168.10.102",
            "sigenergy_charger_slave_id": 247,
        },
    )

    config = config_module.resolve_sigenergy_charger_connection(entry, hass=hass)

    assert config["host"] == "192.168.10.103"
    assert config["slave_id"] == 2


def test_connection_falls_back_to_existing_modbus_host_without_vehicle_config(config_module):
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={
            "sigenergy_modbus_host": "192.168.10.90",
            "sigenergy_charger_type": "evdc",
        },
    )

    config = config_module.resolve_sigenergy_charger_connection(entry)

    assert config["host"] == "192.168.10.90"
    assert config["slave_id"] == 1
