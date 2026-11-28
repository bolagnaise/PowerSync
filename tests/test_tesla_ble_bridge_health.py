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


def _entry(prefixes="teslable"):
    return SimpleNamespace(data={"ev_provider": "both", "tesla_ble_entity_prefix": prefixes}, options={})


def test_old_bridge_firmware_raises_update_notice_with_its_cap(issues):
    """Live 2026-11-28: a bridge built with 15 A capped every BLE mode silently."""
    hass = _hass({"number.teslable_charging_amps": _state(15, max=15)})
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


def test_missing_bridge_and_fleet_only_installs_raise_nothing(issues):
    asyncio.run(health.async_check_tesla_ble_bridges(_hass({}), _entry()))
    fleet = SimpleNamespace(data={"ev_provider": "fleet_api"}, options={})
    asyncio.run(health.async_check_tesla_ble_bridges(
        _hass({"number.teslable_charging_amps": _state(15, max=15)}), fleet))
    assert issues == []


def test_each_configured_bridge_is_checked(issues):
    hass = _hass({
        "number.teslable_charging_amps": _state(15, max=15),
        "number.second_charging_amps": _state(16, max=48),
        "sensor.second_charge_current_request_max": _state(16),
    })
    asyncio.run(health.async_check_tesla_ble_bridges(hass, _entry("teslable, second")))
    assert [c[1] for c in issues if c[0] == "create"] == ["tesla_ble_bridge_update_teslable"]
