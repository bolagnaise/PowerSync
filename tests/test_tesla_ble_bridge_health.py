"""Repairs notices for Tesla BLE bridges whose firmware caps charging current."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from conftest import load_power_sync_submodule

health = load_power_sync_submodule("tesla_ble_bridge_health", package="ble_bridge_health")


class _States:
    def __init__(self, states):
        self._states = states

    def get(self, entity_id):
        return self._states.get(entity_id)

    def async_all(self):
        return [SimpleNamespace(entity_id=k, **vars(v)) for k, v in self._states.items()]


def _state(value, **attributes):
    return SimpleNamespace(state=str(value), attributes=attributes)


@pytest.fixture
def issues(monkeypatch):
    calls = []
    registry = SimpleNamespace(
        IssueSeverity=SimpleNamespace(WARNING="warning"),
        async_create_issue=lambda hass, domain, issue_id, **kw: calls.append(("create", issue_id, kw)),
        async_delete_issue=lambda hass, domain, issue_id: calls.append(("delete", issue_id, None)),
    )
    entity_registry = SimpleNamespace(async_get=lambda hass: hass._registry)
    helpers = SimpleNamespace(issue_registry=registry, entity_registry=entity_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.issue_registry", registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", entity_registry)
    return calls


def _hass(states):
    ids = {entity_id: SimpleNamespace(entity_id=entity_id) for entity_id in states}
    return SimpleNamespace(states=_States(states), _registry=SimpleNamespace(entities=ids))


def _pedro(prefix="teslable"):
    """An entity only the PedroKTFC firmware family publishes."""
    return {f"number.{prefix}_poll_data_period": _state(60)}


def _entry(prefixes="teslable"):
    return SimpleNamespace(data={"ev_provider": "both", "tesla_ble_entity_prefix": prefixes}, options={})


def test_old_bridge_firmware_raises_update_notice_with_its_cap(issues):
    """Live 2026-11-28: a bridge built with 15 A capped every BLE mode silently."""
    hass = _hass({"number.teslable_charging_amps": _state(15, max=15), **_pedro()})
    asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry()))
    created = [c for c in issues if c[0] == "create"]
    assert [c[1] for c in created] == ["tesla_ble_bridge_update_teslable"]
    assert created[0][2]["translation_placeholders"]["amps_max"] == "15"
    assert created[0][2]["learn_more_url"] == health.GUIDE_URL


def test_updated_bridge_below_car_limit_raises_cap_notice(issues):
    hass = _hass({
        "number.teslable_charging_amps": _state(15, max=15),
        "sensor.teslable_charge_current_request_max": _state(32),
    })
    asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry()))
    assert ("delete", "tesla_ble_bridge_update_teslable", None) in issues
    capped = [c for c in issues if c[0] == "create"]
    assert [c[1] for c in capped] == ["tesla_ble_bridge_capped_teslable"]
    assert capped[0][2]["translation_placeholders"]["car_limit"] == "32"


def test_updated_bridge_at_or_above_car_limit_clears_both_notices(issues):
    hass = _hass({
        "number.teslable_charging_amps": _state(32, max=48),
        "sensor.teslable_charge_current_request_max": _state(15),
    })
    asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry()))
    assert not [c for c in issues if c[0] == "create"]
    assert {c[1] for c in issues if c[0] == "delete"} == {
        "tesla_ble_bridge_update_teslable", "tesla_ble_bridge_capped_teslable"}


def test_installer_bridge_named_after_its_mac_is_found_without_a_prefix(issues):
    """Installer images are named ``Tesla BLE <mac>``; a lone bridge is discovered."""
    hass = _hass({
        "sensor.tesla_ble_dd95d4_charging_state": _state("Stopped"),
        "number.tesla_ble_dd95d4_charging_amps": _state(16, max=48),
        "sensor.tesla_ble_dd95d4_charge_current_request_max": _state(32),
    })
    entry = SimpleNamespace(data={"ev_provider": "tesla_ble"}, options={})
    results = asyncio.run(health.async_check_tesla_ble_bridges(hass, entry))
    assert [r.prefix for r in results] == ["tesla_ble_dd95d4"]
    assert not results[0].needs_update and not results[0].capped


def test_missing_bridge_and_fleet_only_installs_raise_nothing(issues):
    asyncio.run(health.async_check_tesla_ble_bridges(_hass({}), _entry()))
    fleet = SimpleNamespace(data={"ev_provider": "fleet_api"}, options={})
    asyncio.run(health.async_check_tesla_ble_bridges(
        _hass({"number.teslable_charging_amps": _state(15, max=15)}), fleet))
    assert issues == []


def test_each_configured_bridge_is_checked(issues):
    hass = _hass({
        "number.teslable_charging_amps": _state(15, max=15),
        **_pedro(),
        "number.second_charging_amps": _state(16, max=48),
        "sensor.second_charge_current_request_max": _state(16),
    })
    asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry("teslable, second")))
    assert [c[1] for c in issues if c[0] == "create"] == ["tesla_ble_bridge_update_teslable"]


def _vin_hass(monkeypatch, states, vins):
    hass = _hass(states)
    calls = []

    async def call(domain, service, data, blocking=False):
        calls.append((domain, service, data))

    hass.services = SimpleNamespace(async_call=call)
    monkeypatch.setattr(health, "known_tesla_vins", lambda _hass: list(vins))
    return hass, calls


def test_installer_bridge_without_vin_gets_the_single_known_car(monkeypatch):
    hass, calls = _vin_hass(monkeypatch, {"text.tesla_ble_tesla_vin": _state("")}, ["5YJ3E1EA7KF000001"])
    entry = SimpleNamespace(data={"ev_provider": "both", "tesla_ble_entity_prefix": "tesla_ble"}, options={})
    written = asyncio.run(health.async_fill_runtime_vins(hass, entry))
    assert written == {"tesla_ble": "5YJ3E1EA7KF000001"}
    assert calls == [("text", "set_value", {"entity_id": "text.tesla_ble_tesla_vin", "value": "5YJ3E1EA7KF000001"})]


def test_vin_fill_leaves_a_set_vin_and_fixed_firmware_alone(monkeypatch):
    hass, calls = _vin_hass(monkeypatch, {"text.tesla_ble_tesla_vin": _state("5YJYGDEE1LF000002")}, ["5YJ3E1EA7KF000001"])
    entry = SimpleNamespace(data={"ev_provider": "both", "tesla_ble_entity_prefix": "tesla_ble"}, options={})
    assert asyncio.run(health.async_fill_runtime_vins(hass, entry)) == {}
    hass, calls2 = _vin_hass(monkeypatch, {}, ["5YJ3E1EA7KF000001"])
    assert asyncio.run(health.async_fill_runtime_vins(hass, entry)) == {}
    assert calls == calls2 == []


def test_vin_fill_uses_explicit_mapping_and_skips_ambiguous_cars(monkeypatch):
    states = {"text.a_tesla_vin": _state(""), "text.b_tesla_vin": _state("unknown")}
    hass, calls = _vin_hass(monkeypatch, states, ["5YJ3E1EA7KF000001", "5YJYGDEE1LF000002"])
    mapped = SimpleNamespace(data={"ev_provider": "both", "tesla_ble_entity_prefix": "a,b",
                                   "tesla_ble_vehicle_mapping": "5YJYGDEE1LF000002=b"}, options={})
    written = asyncio.run(health.async_fill_runtime_vins(hass, mapped))
    assert written.get("b") == "5YJYGDEE1LF000002"
    assert "a" not in written or written["a"] != "5YJYGDEE1LF000002"


def test_yoziru_bridge_is_not_told_to_switch_to_the_pedro_firmware(issues):
    """Codex review: the companion update exists for the Pedro family only."""
    hass = _hass({
        "number.teslable_charging_amps": _state(15, max=15),
        "sensor.teslable_battery": _state(60),
        "sensor.teslable_charging": _state("Stopped"),
    })
    results = asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry()))
    assert not results[0].pedro_family and not results[0].needs_update
    assert not [c for c in issues if c[0] == "create"]
    # A notice raised before this check (older release) is cleared.
    assert ("delete", "tesla_ble_bridge_update_teslable", None) in issues


def test_resolved_bridge_prefix_is_checked(issues):
    """Codex review: configured tesla_ble resolving to garage_ble still warns."""
    hass = _hass({
        "sensor.garage_ble_charging_state": _state("Charging"),
        "number.garage_ble_charging_amps": _state(15, max=15),
        **_pedro("garage_ble"),
    })
    entry = SimpleNamespace(data={"ev_provider": "tesla_ble"}, options={})
    asyncio.run(health.async_check_tesla_ble_bridges(hass, entry))
    assert [c[1] for c in issues if c[0] == "create"] == ["tesla_ble_bridge_update_garage_ble"]
