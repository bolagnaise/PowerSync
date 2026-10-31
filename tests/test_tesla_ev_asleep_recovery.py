"""Bounded Fleet wake/read recovery after a local Tesla producer fails."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_ev_ocpp_actions import _Hass, _State, actions


VIN = "5YJ3E1EA7NF0000A1"
COMMAND = "switch.fleet_charge"
WAKE = "button.fleet_wake"


def _fleet_registry(
    *,
    include_wake: bool,
    platform: str = "tesla_fleet",
    command: str = COMMAND,
    wake: str = WAKE,
    config_entry_id: str = "fleet-entry",
    device_id: str = "fleet-device",
    command_unique_id: str = "fleet-charge",
    wake_unique_id: str | None = None,
):
    entities = {
        command: SimpleNamespace(
            entity_id=command,
            platform=platform,
            config_entry_id=config_entry_id,
            device_id=device_id,
            unique_id=command_unique_id,
            disabled_by=None,
        )
    }
    if include_wake:
        entities[wake] = SimpleNamespace(
            entity_id=wake,
            platform=platform,
            config_entry_id=config_entry_id,
            device_id=device_id,
            unique_id=wake_unique_id or f"{VIN}-wake",
            disabled_by=None,
        )
    return entities


def _recovery_route(
    *,
    platform: str = "tesla_fleet",
    command: str = COMMAND,
    config_entry_id: str = "fleet-entry",
    device_id: str = "fleet-device",
    command_unique_id: str = "fleet-charge",
):
    return {
        "kind": "ha",
        "entity_id": command,
        "entity_identity": {
            "platform": platform,
            "config_entry_id": config_entry_id,
            "device_id": device_id,
            "unique_id": command_unique_id,
        },
    }


def _patch_action_seams(
    monkeypatch, hass, *, observation, route, events, command: str = COMMAND
):
    """Keep this regression at the post-isolation action boundary."""
    monkeypatch.setattr(actions.er, "async_get", lambda value: value.entity_registry)
    monkeypatch.setattr(actions, "_get_ev_config", lambda entry: {
        "ev_provider": actions.EV_PROVIDER_FLEET_API,
    })
    monkeypatch.setattr(actions, "_resolve_ble_prefix_for_vehicle", lambda *args: None)
    monkeypatch.setattr(actions, "bind_vehicle", lambda *args: None)
    monkeypatch.setattr(actions, "_tesla_transport_route", AsyncMock(return_value=route))
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *args, **kwargs: lambda: True)
    monkeypatch.setattr(
        actions,
        "_fresh_tesla_command_observation",
        AsyncMock(side_effect=lambda *args, **kwargs: (events.append("observe"), observation)[1]),
    )
    monkeypatch.setattr(actions, "_tesla_ha_command_entity", AsyncMock(return_value=command))
    monkeypatch.setattr(actions, "_wake_tesla_ev_for_command", AsyncMock(return_value=True))
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *args: True)
    monkeypatch.setattr(actions, "prepare_ev_submission", AsyncMock(return_value=True))

    async def service_call(domain, service, data, blocking=True):
        events.append(f"{domain}.{service}")
        hass.services.calls.append((domain, service, data))

    monkeypatch.setattr(hass.services, "async_call", service_call)


@pytest.mark.asyncio
async def test_fleet_recovery_wakes_then_reads_before_charge_start(monkeypatch):
    """A Fleet Wake entity may wake an asleep coordinator before Start."""
    entities = _fleet_registry(include_wake=True)
    hass = _Hass(
        [_State(COMMAND, "off"), _State(WAKE, "unknown")],
        entities,
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    events = []
    observation = SimpleNamespace(
        charge_state="Stopped",
        charger_actual_current=0,
        charger_power=0,
    )
    _patch_action_seams(monkeypatch, hass, observation=observation,
                        route=_recovery_route(), events=events)
    reads = iter((None, observation))

    async def recover_read(*args, **kwargs):
        events.append("observe")
        return next(reads)

    monkeypatch.setattr(actions, "_fresh_tesla_command_observation", recover_read)

    result = await actions._action_start_ev_charging.__wrapped__(
        hass,
        entry,
        {"charger_type": "tesla", "vehicle_vin": VIN},
    )

    assert result is True
    assert events == ["observe", "button.press", "observe", "switch.turn_on"]
    assert hass.services.calls == [
        ("button", "press", {"entity_id": WAKE}),
        ("switch", "turn_on", {"entity_id": COMMAND}),
    ]


@pytest.mark.asyncio
async def test_fresh_fleet_observation_skips_recovery_wake(monkeypatch):
    """A fresh physical sample must not issue a duplicate wake or Start."""
    hass = _Hass(
        [_State(COMMAND, "on"), _State(WAKE, "unknown")],
        _fleet_registry(include_wake=True),
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    events = []
    observation = SimpleNamespace(
        charge_state="Charging",
        charger_actual_current=8,
        charger_power=1800,
    )
    _patch_action_seams(monkeypatch, hass, observation=observation,
                        route=_recovery_route(), events=events)

    result = await actions._action_start_ev_charging.__wrapped__(
        hass,
        entry,
        {"charger_type": "tesla", "vehicle_vin": VIN},
    )

    assert result is True
    assert events == ["observe"]
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_native_teslemetry_recovery_uses_renamed_native_wake_over_fleet_decoy(monkeypatch):
    """Native Teslemetry wake identity wins over a same-VIN Fleet decoy."""
    native_command = "switch.native_charge"
    native_wake = "button.vehicle_wake_alias"
    entities = _fleet_registry(
        include_wake=True,
        platform="teslemetry",
        command=native_command,
        wake=native_wake,
        config_entry_id="native-entry",
        device_id="native-device",
        command_unique_id="native-charge",
    )
    entities["button.fleet_decoy_wake"] = SimpleNamespace(
        entity_id="button.fleet_decoy_wake",
        platform="tesla_fleet",
        config_entry_id="fleet-entry",
        device_id="fleet-device",
        unique_id=f"{VIN}-wake",
        disabled_by=None,
    )
    hass = _Hass(
        [_State(native_command, "off"), _State(native_wake, "unknown"),
         _State("button.fleet_decoy_wake", "unknown")],
        entities,
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    events = []
    observation = SimpleNamespace(
        charge_state="Stopped",
        charger_actual_current=0,
        charger_power=0,
    )
    _patch_action_seams(
        monkeypatch,
        hass,
        observation=None,
        route=_recovery_route(
            platform="teslemetry",
            command=native_command,
            config_entry_id="native-entry",
            device_id="native-device",
            command_unique_id="native-charge",
        ),
        events=events,
        command=native_command,
    )
    reads = iter((None, observation))

    async def recover_read(*args, **kwargs):
        events.append("observe")
        return next(reads)

    monkeypatch.setattr(actions, "_fresh_tesla_command_observation", recover_read)
    result = await actions._action_start_ev_charging.__wrapped__(
        hass,
        entry,
        {"charger_type": "tesla", "vehicle_vin": VIN},
    )

    assert result is True
    assert events == ["observe", "button.press", "observe", "switch.turn_on"]
    assert hass.services.calls == [
        ("button", "press", {"entity_id": native_wake}),
        ("switch", "turn_on", {"entity_id": native_command}),
    ]


@pytest.mark.asyncio
async def test_fleet_recovery_without_wake_entity_never_uses_charge_as_wake_probe(monkeypatch):
    """Vehicle Charging Commands alone cannot safely be used as a wake probe."""
    hass = _Hass(
        [_State(COMMAND, "off")],
        _fleet_registry(include_wake=False),
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    params = {"charger_type": "tesla", "vehicle_vin": VIN}
    events = []
    _patch_action_seams(monkeypatch, hass, observation=None,
                        route=_recovery_route(), events=events)

    result = await actions._action_start_ev_charging.__wrapped__(hass, entry, params)

    assert result is None
    assert params["_tesla_transport_recovery_pending"] is True
    assert events == ["observe"]
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_fleet_recovery_wake_timeout_stays_pending_without_start(monkeypatch):
    """A bounded wake timeout cannot fall through to a blind charge Start."""
    hass = _Hass(
        [_State(COMMAND, "off"), _State(WAKE, "unknown")],
        _fleet_registry(include_wake=True),
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    params = {"charger_type": "tesla", "vehicle_vin": VIN}
    events = []
    _patch_action_seams(monkeypatch, hass, observation=None,
                        route=_recovery_route(), events=events)

    async def timeout_wake(domain, service, data, blocking=True):
        events.append(f"{domain}.{service}")
        if domain == "button":
            raise TimeoutError("wake deadline")

    monkeypatch.setattr(hass.services, "async_call", timeout_wake)
    result = await actions._action_start_ev_charging.__wrapped__(hass, entry, params)

    assert result is None
    assert params["_tesla_transport_recovery_pending"] is True
    assert events == ["observe", "button.press"]


@pytest.mark.asyncio
async def test_newly_isolated_ble_fallback_repins_fleet_wake_read_then_starts(monkeypatch):
    """Isolation completed by this request must use its newly pinned HA route."""
    hass = _Hass(
        [_State(COMMAND, "off"), _State(WAKE, "unknown")],
        _fleet_registry(include_wake=True),
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    events = []
    observation = SimpleNamespace(
        charge_state="Stopped",
        charger_actual_current=0,
        charger_power=0,
    )
    _patch_action_seams(monkeypatch, hass, observation=None,
                        route=None, events=events)
    monkeypatch.setattr(actions, "_get_ev_config", lambda entry: {
        "ev_provider": actions.EV_PROVIDER_BOTH,
    })
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(
        actions,
        "_run_ble_transport_command",
        AsyncMock(return_value=(None, True)),
    )
    routes = iter((None, _recovery_route()))
    monkeypatch.setattr(
        actions,
        "_tesla_transport_route",
        AsyncMock(side_effect=lambda *args, **kwargs: next(routes)),
    )
    reads = iter((None, observation))

    async def recover_read(*args, **kwargs):
        events.append("observe")
        return next(reads)

    monkeypatch.setattr(actions, "_fresh_tesla_command_observation", recover_read)
    result = await actions._action_start_ev_charging.__wrapped__(
        hass,
        entry,
        {"charger_type": "tesla", "vehicle_vin": VIN},
    )

    assert result is True
    assert events == ["observe", "button.press", "observe", "switch.turn_on"]
    assert hass.services.calls == [
        ("button", "press", {"entity_id": WAKE}),
        ("switch", "turn_on", {"entity_id": COMMAND}),
    ]


@pytest.mark.asyncio
async def test_fleet_recovery_deadline_revoked_after_wake_stays_pending(monkeypatch):
    """Owner/deadline revocation after Wake prevents the eventual Start."""
    hass = _Hass(
        [_State(COMMAND, "off"), _State(WAKE, "unknown")],
        _fleet_registry(include_wake=True),
    )
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    params = {
        "charger_type": "tesla",
        "vehicle_vin": VIN,
        "_boost_start_deadline": time.time() + 30,
    }
    events = []
    _patch_action_seams(monkeypatch, hass, observation=None,
                        route=_recovery_route(), events=events)

    def authority_after_wake(*args, **kwargs):
        return lambda: params["_boost_start_deadline"] > time.time()

    monkeypatch.setattr(actions, "_tesla_transport_authority", authority_after_wake)

    async def revoke_on_wake(domain, service, data, blocking=True):
        events.append(f"{domain}.{service}")
        params["_boost_start_deadline"] = time.time() - 1

    monkeypatch.setattr(hass.services, "async_call", revoke_on_wake)
    result = await actions._action_start_ev_charging.__wrapped__(hass, entry, params)

    assert result is None
    assert params["_tesla_transport_recovery_pending"] is True
    assert events == ["observe", "button.press"]


@pytest.mark.asyncio
async def test_unisolated_missing_observation_never_wakes_or_starts(monkeypatch):
    """Without a durable isolation fence, missing readback cannot fall through."""
    hass = _Hass([_State(COMMAND, "off")], _fleet_registry(include_wake=True))
    entry = SimpleNamespace(entry_id="entry-1", data={}, options={})
    params = {"charger_type": "tesla", "vehicle_vin": VIN}
    events = []
    _patch_action_seams(monkeypatch, hass, observation=None,
                        route=None, events=events)
    monkeypatch.setattr(actions, "_get_ev_config", lambda entry: {
        "ev_provider": actions.EV_PROVIDER_BOTH,
    })
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: True)
    monkeypatch.setattr(
        actions,
        "_run_ble_transport_command",
        AsyncMock(return_value=(None, False)),
    )

    result = await actions._action_start_ev_charging.__wrapped__(hass, entry, params)

    assert result is None
    assert params["_tesla_transport_recovery_pending"] is True
    assert events == ["observe"]
    assert hass.services.calls == []
