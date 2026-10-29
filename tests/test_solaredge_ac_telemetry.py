"""SolarEdge AC card totals use explicit, fresh inverter members only."""

import ast
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 29, 3, tzinfo=timezone.utc)
LEADER = "sensor.solaredge_leader_ac_power"
FOLLOWER = "sensor.solaredge_follower_ac_power"


def reading(value, unit="W", age=0):
    return SimpleNamespace(
        state=str(value), attributes={"unit_of_measurement": unit},
        last_reported=NOW - timedelta(seconds=age),
        # An unchanged value may have been freshly reported.
        last_updated=NOW - timedelta(days=1),
    )


def poll_sensor(monkeypatch, members, readings, brand="solaredge"):
    """Execute the actual poll with a read-only fake single-target controller."""
    source = ROOT / "custom_components/power_sync/sensor.py"
    tree = ast.parse(source.read_text())
    cls = next(n for n in tree.body if getattr(n, "name", None) == "InverterStatusSensor")
    poll = next(n for n in cls.body if getattr(n, "name", None) == "_async_poll_inverter_once")
    merge = next(n for n in tree.body if getattr(n, "name", None) == "_merge_inverter_status_attributes")
    state = SimpleNamespace(
        status=SimpleNamespace(value="online"), is_curtailed=False,
        power_output_w=4281, power_limit_percent=100,
        attributes={"ac_power_w": 4281, "dc_power_w": 4346},
    )
    controller = SimpleNamespace(get_status=AsyncMock(return_value=state))
    package = ModuleType("ticket449")
    package.__path__ = [str(ROOT / "custom_components/power_sync")]
    inverters = ModuleType("ticket449.inverters")
    inverters.__path__ = [str(ROOT / "custom_components/power_sync/inverters")]
    inverters.get_inverter_controller = lambda **kw: controller
    monkeypatch.setitem(sys.modules, "ticket449", package)
    monkeypatch.setitem(sys.modules, "ticket449.inverters", inverters)
    namespace = {n.id: n.id for n in ast.walk(poll) if isinstance(n, ast.Name) and n.id.isupper()}
    namespace.update(
        __package__="ticket449", Any=object, DOMAIN="power_sync",
        _LOGGER=logging.getLogger(__name__), dt_util=SimpleNamespace(now=lambda: NOW),
        _sungrow_inverter_source_id=lambda *args: None,
        INVERTER_CONTROL_MODES=(),
    )
    exec(compile(ast.fix_missing_locations(ast.Module(body=[merge, poll], type_ignores=[])), str(source), "exec"), namespace)
    config = {
        "CONF_AC_INVERTER_CURTAILMENT_ENABLED": True,
        "CONF_INVERTER_BRAND": brand, "CONF_INVERTER_HOST": "example.invalid",
        "CONF_SOLAREDGE_AC_POWER_ENTITIES": members,
    }
    shared = {"exists": True}
    sensor = SimpleNamespace(
        _controller=None, _offline_count=0, _cached_attrs={},
        _entry=SimpleNamespace(entry_id="entry"),
        _get_config_value=lambda key, default=None: config.get(key, default),
        hass=SimpleNamespace(states=SimpleNamespace(get=readings.get), data={"power_sync": {"entry": shared}}),
        async_write_ha_state=lambda: None,
    )
    sensor.poll = lambda: asyncio.run(namespace["_async_poll_inverter_once"](sensor))
    sensor.poll()
    return sensor, state, shared


def test_reported_leader_follower_total_is_display_only(monkeypatch):
    sensor, controller_state, shared = poll_sensor(
        monkeypatch, [LEADER, FOLLOWER, LEADER],
        {LEADER: reading(4281), FOLLOWER: reading(8.9, "kW")},
    )
    assert sensor._cached_attrs["power_output_w"] == 13181
    assert shared["inverter_attributes"]["power_output_w"] == 13181
    assert controller_state.power_output_w == 4281
    assert sensor._cached_attrs["ac_power_w"] == 4281
    assert sensor._cached_attrs["dc_power_w"] == 4346
    assert len(sensor._cached_attrs["ac_power_members"]) == 2


@pytest.mark.parametrize("members,brand", [([], "solaredge"), ([LEADER, FOLLOWER], "fronius")])
def test_unconfigured_single_inverter_and_other_brands_are_unchanged(monkeypatch, members, brand):
    sensor, _, _ = poll_sensor(monkeypatch, members, {}, brand)
    assert sensor._cached_attrs["power_output_w"] == 4281
    assert "ac_power_complete" not in sensor._cached_attrs


@pytest.mark.parametrize("bad", [None, reading("unavailable"), reading("NaN"), reading("inf"), reading(5, "kWh"), reading(8900, age=121), reading(8900, age=-1)])
def test_missing_invalid_or_stale_member_never_leaves_a_partial_or_cached_total(monkeypatch, bad):
    readings = {LEADER: reading(4281), FOLLOWER: reading(8900)}
    sensor, _, shared = poll_sensor(monkeypatch, [LEADER, FOLLOWER], readings)
    assert sensor._cached_attrs["power_output_w"] == 13181
    if bad is None:
        del readings[FOLLOWER]
    else:
        readings[FOLLOWER] = bad
    sensor.poll()
    assert sensor._cached_attrs["power_output_w"] is None
    assert shared["inverter_attributes"]["ac_power_complete"] is False
    assert sensor._cached_attrs["ac_power_members"][1]["error"]
    readings[FOLLOWER] = reading(0)
    sensor.poll()
    assert sensor._cached_attrs["power_output_w"] == 4281
    assert sensor._cached_attrs["ac_power_complete"] is True


@pytest.mark.parametrize("members", ["sensor.bad", ["number.bad"], [None]])
def test_malformed_persisted_member_configuration_fails_closed(monkeypatch, members):
    sensor, _, _ = poll_sensor(monkeypatch, members, {})
    assert sensor._cached_attrs["power_output_w"] is None
    assert sensor._cached_attrs["ac_power_complete"] is False


def test_card_shows_incomplete_before_sleep_and_displays_a_complete_zero_total():
    source = (ROOT / "custom_components/power_sync/frontend/power-sync-strategy.js").read_text()
    card = source[source.index("if (hasAC) {"):]
    label = card.split("label: `[[[", 1)[1].split("]]]`", 1)[0].replace("${acEntity}", "sensor.ac")
    script = "const render = new Function('states', " + json.dumps(label) + ");\n"
    for state, complete, power, expected in [
        ("running", False, None, "Telemetry incomplete"),
        ("offline", False, None, "Offline - Cannot reach - Telemetry incomplete"),
        ("error", False, None, "Error - Check logs - Telemetry incomplete"),
        ("running", True, 0, "0W"), ("running", True, 13181, "13181W"),
    ]:
        states = {"sensor.ac": {"state": state, "attributes": {"brand": "solaredge", "power_output_w": power, "ac_power_complete": complete}}, "sun.sun": {"state": "above_horizon" if complete else "below_horizon"}}
        script += f"if (!render({json.dumps(states)}).includes({json.dumps(expected)})) throw Error('Bad card label');\n"
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("members,expected", [([LEADER, FOLLOWER, LEADER], [LEADER, FOLLOWER]), ([], [])])
def test_options_save_and_clear_explicit_member_selection(monkeypatch, members, expected):
    # Install the same isolated relative-import package as the poll regression.
    poll_sensor(monkeypatch, [], {})
    source = ROOT / "custom_components/power_sync/config_flow.py"
    cls = next(n for n in ast.parse(source.read_text()).body if getattr(n, "name", None) == "PowerSyncOptionsFlow")
    method = next(n for n in cls.body if getattr(n, "name", None) == "async_step_inverter_config")
    namespace = {n.id: n.id for n in ast.walk(method) if isinstance(n, ast.Name) and n.id.isupper()}
    namespace.update(__package__="ticket449", Any=object, FlowResult=dict)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[])), str(source), "exec"), namespace)
    saved = {}
    flow = SimpleNamespace(
        _inverter_brand="solaredge", _from_menu=True,
        config_entry=SimpleNamespace(data={}),
        _save_and_finish=lambda data: saved.update(data) or data,
        _get_option=lambda key, default=None: default,
    )
    asyncio.run(namespace[method.name](flow, {
        "CONF_INVERTER_HOST": "example.invalid",
        "CONF_SOLAREDGE_AC_POWER_ENTITIES": members,
        "CONF_INVERTER_RATED_POWER_W": 5000,
    }))
    assert saved["CONF_SOLAREDGE_AC_POWER_ENTITIES"] == expected
    assert saved["CONF_INVERTER_RATED_POWER_W"] == 5000
    assert saved["CONF_AC_INVERTER_CURTAILMENT_ENABLED"] is True


@pytest.mark.parametrize("members,expected", [(None, "invalid_member_configuration"), ([], None)])
def test_config_validation_and_runtime_agree(monkeypatch, members, expected):
    poll_sensor(monkeypatch, [], {})
    module = sys.modules.get("ticket449.inverters.solaredge_ac_telemetry")
    if module is None:
        import importlib
        module = importlib.import_module("ticket449.inverters.solaredge_ac_telemetry")
    if expected:
        with pytest.raises(ValueError):
            module.normalize_ac_power_entities(members)
        assert module.read_ac_power_members(SimpleNamespace(), members, now=NOW)["ac_power_error"] == expected
    else:
        assert module.normalize_ac_power_entities(members) == []
