"""Tests for the PowerSync Cloud flow reporter payload builder.

`build_payload()` is a pure function (no Home Assistant object required), so
it's imported directly from the real module file with a minimal stub of
`power_sync.const` -- mirroring the pattern in test_auto_update.py.
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_ps_const = types.ModuleType("power_sync.const")
_ps_const.CONF_CLOUD_FLOW_GRID_ENTITY = "cloud_flow_grid_entity"
_ps_const.CONF_CLOUD_FLOW_SOLAR_ENTITY = "cloud_flow_solar_entity"
_ps_const.CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY = "cloud_flow_battery_power_entity"
_ps_const.CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY = "cloud_flow_battery_soc_entity"
_ps_const.CONF_CLOUD_FLOW_LOAD_ENTITY = "cloud_flow_load_entity"
_ps_const.CONF_CLOUD_FLOW_INVERT_GRID = "cloud_flow_invert_grid"
_ps_const.DEFAULT_CLOUD_FLOW_INTERVAL = 30
_ps_const.DOMAIN = "power_sync"
_ps_const.POWERSYNC_FLOW_API_URL = "https://api.powersync.cc/v1/flow"
_ps_const.TESLA_PROVIDER_POWERSYNC = "powersync"
sys.modules["power_sync.const"] = _ps_const

sys.modules.pop("power_sync.cloud_flow_reporter", None)
cloud_flow_reporter = importlib.import_module("power_sync.cloud_flow_reporter")

build_payload = cloud_flow_reporter.build_payload

GRID = "cloud_flow_grid_entity"
SOLAR = "cloud_flow_solar_entity"
BATTERY_POWER = "cloud_flow_battery_power_entity"
BATTERY_SOC = "cloud_flow_battery_soc_entity"
LOAD = "cloud_flow_load_entity"
INVERT = "cloud_flow_invert_grid"

GRID_EID = "sensor.grid_power"
SOLAR_EID = "sensor.solar_power"
BATTERY_POWER_EID = "sensor.battery_power"
BATTERY_SOC_EID = "sensor.battery_level"
LOAD_EID = "sensor.home_load"


class _State:
    """Duck-typed stand-in for homeassistant.core.State."""

    def __init__(self, state, unit=None, last_updated=None):
        self.state = state
        self.attributes = {} if unit is None else {"unit_of_measurement": unit}
        self.last_updated = last_updated


def _base_options(**overrides):
    options = {
        GRID: GRID_EID,
        SOLAR: None,
        BATTERY_POWER: None,
        BATTERY_SOC: None,
        LOAD: None,
        INVERT: False,
    }
    options.update(overrides)
    return options


def test_watts_to_kw_conversion():
    """A W-unit power sensor is divided by 1000 into kW."""
    states = {GRID_EID: _State("1500", unit="W")}
    payload = build_payload(states, _base_options())
    assert payload is not None
    assert payload["net_import_kw"] == 1.5


def test_kw_passthrough():
    """A kW-unit power sensor is passed through unchanged."""
    states = {GRID_EID: _State("2.4", unit="kW")}
    payload = build_payload(states, _base_options())
    assert payload is not None
    assert payload["net_import_kw"] == 2.4


def test_battery_soc_percent_to_fraction():
    """Battery SoC (0-100 %) is converted to a 0-1 fraction."""
    states = {
        GRID_EID: _State("500", unit="W"),
        BATTERY_SOC_EID: _State("72", unit="%"),
    }
    options = _base_options(**{BATTERY_SOC: BATTERY_SOC_EID})
    payload = build_payload(states, options)
    assert payload is not None
    assert payload["battery_soc"] == 0.72


def test_battery_soc_fraction_is_clamped():
    """Out-of-range SoC readings are clamped to [0, 1]."""
    states = {
        GRID_EID: _State("500", unit="W"),
        BATTERY_SOC_EID: _State("150", unit="%"),
    }
    options = _base_options(**{BATTERY_SOC: BATTERY_SOC_EID})
    payload = build_payload(states, options)
    assert payload is not None
    assert payload["battery_soc"] == 1.0


def test_invert_grid_sign():
    """CONF_CLOUD_FLOW_INVERT_GRID flips the sign of net_import_kw."""
    states = {GRID_EID: _State("1000", unit="W")}
    options = _base_options(**{INVERT: True})
    payload = build_payload(states, options)
    assert payload is not None
    assert payload["net_import_kw"] == -1.0


def test_skip_entire_push_when_grid_unavailable():
    """The whole payload is skipped (None) when the grid entity is unavailable."""
    states = {GRID_EID: _State("unavailable", unit="W")}
    payload = build_payload(states, _base_options())
    assert payload is None


def test_skip_entire_push_when_grid_missing():
    """Missing grid entity state (not in the dict) also skips the push."""
    payload = build_payload({}, _base_options())
    assert payload is None


def test_skip_field_on_unknown_unit():
    """An optional field with an unrecognized unit is omitted, not the whole push."""
    states = {
        GRID_EID: _State("500", unit="W"),
        SOLAR_EID: _State("3", unit="A"),  # amps -- not W or kW
    }
    options = _base_options(**{SOLAR: SOLAR_EID})
    payload = build_payload(states, options)
    assert payload is not None
    assert "production_kw" not in payload
    assert payload["net_import_kw"] == 0.5


def test_optional_fields_included_when_configured_and_available():
    """Solar/battery-power/load are included in the payload when configured."""
    states = {
        GRID_EID: _State("500", unit="W"),
        SOLAR_EID: _State("2.1", unit="kW"),
        BATTERY_POWER_EID: _State("-800", unit="W"),
        LOAD_EID: _State("1.2", unit="kW"),
    }
    options = _base_options(
        **{
            SOLAR: SOLAR_EID,
            BATTERY_POWER: BATTERY_POWER_EID,
            LOAD: LOAD_EID,
        }
    )
    payload = build_payload(states, options)
    assert payload is not None
    assert payload["production_kw"] == 2.1
    assert payload["battery_discharge_kw"] == -0.8
    assert payload["consumption_kw"] == 1.2


def test_optional_field_skipped_when_not_configured():
    """An unconfigured optional entity is simply absent from the payload."""
    states = {GRID_EID: _State("500", unit="W")}
    payload = build_payload(states, _base_options())
    assert payload is not None
    assert "production_kw" not in payload
    assert "battery_discharge_kw" not in payload
    assert "battery_soc" not in payload
    assert "consumption_kw" not in payload


def test_source_id_is_default():
    """The payload always reports the single 'default' source."""
    states = {GRID_EID: _State("500", unit="W")}
    payload = build_payload(states, _base_options())
    assert payload is not None
    assert payload["source_id"] == "default"


def test_tsms_from_grid_last_updated():
    """tsms is derived from the grid state's last_updated, in ms epoch."""
    last_updated = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)
    states = {GRID_EID: _State("500", unit="W", last_updated=last_updated)}
    payload = build_payload(states, _base_options())
    assert payload is not None
    assert payload["tsms"] == int(last_updated.timestamp() * 1000)


def test_tsms_falls_back_to_now_without_last_updated():
    """tsms falls back to the current time when last_updated is unavailable."""
    import time

    states = {GRID_EID: _State("500", unit="W", last_updated=None)}
    before = int(time.time() * 1000)
    payload = build_payload(states, _base_options())
    after = int(time.time() * 1000)
    assert payload is not None
    assert before <= payload["tsms"] <= after
