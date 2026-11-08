"""Focused durability checks for native Fleet/Teslemetry Tesla writes."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_ev_ocpp_actions import _Entry, _Hass, _State, actions


VIN = "5YJ3E1EA7NF0000A1"
ENTITY = "switch.car_charge"
AMPS_ENTITY = "number.car_charge_current"
LIMIT_ENTITY = "number.car_charge_limit"


def _native_hass(monkeypatch, platform: str, entity_id=ENTITY):
    import importlib

    registration = SimpleNamespace(
        entity_id=entity_id,
        platform=platform,
        config_entry_id=f"{platform}-entry",
        device_id=f"{platform}-device",
        unique_id=f"{VIN}-charge",
        disabled_by=None,
    )
    device = SimpleNamespace(
        id=registration.device_id,
        identifiers={(platform, VIN)},
        config_entries={registration.config_entry_id},
    )
    hass = _Hass(
        [_State(entity_id, "off")],
        {entity_id: registration},
        {registration.device_id: device},
    )
    # Several full-collection fixtures replace the Home Assistant registry
    # modules in ``sys.modules``.  Replacing the action module's lookup
    # objects as well as their method keeps every lazy identity read in this
    # harness bound to its own fake registries.
    monkeypatch.setattr(
        actions,
        "er",
        SimpleNamespace(async_get=lambda _hass: hass.entity_registry),
    )
    monkeypatch.setattr(
        actions,
        "dr",
        SimpleNamespace(async_get=lambda _hass: hass.device_registry),
    )
    # ``teslemetry_bluetooth_commands`` resolves the registry lazily through
    # Home Assistant imports.  Collection fixtures can leave those import
    # views pointing at another fake registry even though the action module
    # has been rebound above.
    command_module = importlib.import_module(
        "power_sync.teslemetry_bluetooth_commands"
    )
    monkeypatch.setattr(
        command_module,
        "_registry",
        lambda _hass: hass.entity_registry,
    )
    monkeypatch.setattr(
        command_module,
        "_device_for",
        lambda _hass, device_id: hass.device_registry.async_get(device_id),
    )
    return hass


def _native_identity(hass, entity_id=ENTITY):
    registration = hass.entity_registry.async_get(entity_id)
    return {
        key: getattr(registration, key)
        for key in (
            "entity_id",
            "platform",
            "config_entry_id",
            "device_id",
            "unique_id",
            "disabled_by",
        )
    }


def _ble_native_hass(monkeypatch, *, max_amps=16):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    ble_entity = "number.ble_charging_amps"
    ble_device = SimpleNamespace(
        id="ble-device",
        identifiers={("esphome", "ble-device")},
        config_entries={"ble-entry"},
    )
    hass.device_registry.devices[ble_device.id] = ble_device
    hass.entity_registry.entities[ble_entity] = SimpleNamespace(
        entity_id=ble_entity,
        platform="esphome",
        config_entry_id="ble-entry",
        device_id=ble_device.id,
        unique_id="ble-amps",
        disabled_by=None,
    )
    hass.states._states[ble_entity] = _State(
        ble_entity,
        "16",
        {"min": 5, "max": max_amps},
    )
    return hass, ble_entity


def _native_observation(*, requested_current=16, timestamp=None, vin=VIN):
    timestamp = time.time() if timestamp is None else timestamp
    return SimpleNamespace(
        vin=vin,
        domain="tesla_fleet",
        config_entry_id="tesla_fleet-entry",
        device_id="tesla_fleet-device",
        producer_identity=f"tesla_fleet:tesla_fleet-entry:tesla_fleet-device:{vin}",
        source_charge_timestamp=timestamp,
        requested_current=requested_current,
        charger_actual_current=0,
        charger_power=0,
    )


def _journal_mocks(monkeypatch, *, ticket=None, on_begin=None):
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = ticket or SimpleNamespace(attempt_id="attempt-1")
    finished = []

    async def begin(*args, **kwargs):
        if on_begin is not None:
            on_begin()
        return ticket

    async def finish(_ticket, outcome):
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)
    return finished


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry", "tessie"])
@pytest.mark.parametrize("operation,entity,target", [
    ("rate", AMPS_ENTITY, 17), ("limit", LIMIT_ENTITY, 80),
])
async def test_native_numeric_target_is_durable_before_handoff_and_after_reload(
    monkeypatch, platform, operation, entity, target,
):
    """A lost reply must retain the value actually handed to the provider."""
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    hass = _native_hass(monkeypatch, platform, entity)
    entry = _Entry()
    saved = {}
    handoffs = []

    async def persist(_hass, _entry, runtime):
        saved.clear()
        saved.update(deepcopy(runtime[recovery.RECOVERY_KEY]))
        return True

    async def lost_reply(domain, service, data, **kwargs):
        handoffs.append((deepcopy(data), deepcopy(saved)))
        raise TimeoutError("provider reply lost")

    monkeypatch.setattr(recovery, "_persist", persist)
    monkeypatch.setattr(recovery, "_registry", lambda h: h.entity_registry)
    hass.services.async_call = lost_reply
    request = {"entity_id": entity, "value": target}

    async def authorize():
        # Awaiting authority must not alter the captured command or journal.
        request["value"] = 99
        await asyncio.sleep(0)
        return True

    result = await actions._run_native_tesla_service_command(
        hass, entry, {"vehicle_vin": VIN}, operation, entity,
        "number", "set_value", request, authorize,
    )
    assert result.outcome == "sent_unknown"
    assert len(handoffs) == 1
    data, at_handoff = handoffs[0]
    assert data == {"entity_id": entity, "value": target}
    attempt = next(iter(at_handoff["vehicles"][VIN]["attempts"].values()))
    assert attempt["operation"] == operation
    assert attempt["route"]["command_request"] == {
        "operation": operation, "value": target, "unit": "A" if operation == "rate" else "%",
    }
    retained = deepcopy(saved["vehicles"][VIN]["attempts"])
    assert next(iter(retained.values()))["route"]["command_request"]["value"] == target
    hass.data["power_sync"][entry.entry_id] = {
        recovery.RECOVERY_KEY: deepcopy(saved),
    }
    assert recovery.get_vehicle_route(hass, entry, VIN)["kind"] == "blocked"
    assert hass.data["power_sync"][entry.entry_id][recovery.RECOVERY_KEY]["vehicles"][VIN]["attempts"] == retained


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, True, "17", float("nan"), float("inf"), -1])
@pytest.mark.parametrize("operation,entity", [("rate", AMPS_ENTITY), ("limit", LIMIT_ENTITY)])
async def test_native_invalid_numeric_request_never_reaches_journal_or_provider(
    monkeypatch, value, operation, entity,
):
    import importlib
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    begin = AsyncMock()
    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    hass = _native_hass(monkeypatch, "tesla_fleet", entity)
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, operation, entity,
        "number", "set_value", {"entity_id": entity, "value": value}, lambda: True,
    )
    assert result.outcome == "not_sent"
    begin.assert_not_awaited()
    assert not hass.services.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,reason", [
    ("missing", "Tesla finite EV native command entity unavailable or disabled before dispatch"),
    ("disabled", "Tesla finite EV native command entity unavailable or disabled before dispatch"),
    ("malformed", "Tesla finite EV control held by invalid retained recovery history"),
    ("rebound", "Tesla finite EV producer binding changed before dispatch"),
])
async def test_finite_native_refusal_identifies_admission_failure(monkeypatch, failure, reason):
    from datetime import datetime, timedelta, timezone
    from power_sync.automations import ev_ownership

    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    if failure == "missing":
        hass.entity_registry.entities.clear()
    elif failure == "disabled":
        hass.entity_registry.entities[ENTITY].disabled_by = "user"
    elif failure == "malformed":
        hass.data["power_sync"][entry.entry_id]["ev_transport_recovery"] = "invalid"
    else:
        original = actions._tesla_ha_command_entity

        async def rebind_after_discovery(*args):
            entity = await original(*args)
            entry.options = {"ev_provider": "tesla_ble"}
            return entity

        monkeypatch.setattr(actions, "_tesla_ha_command_entity", rebind_after_discovery)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=3)
    params = {
        "vehicle_vin": VIN, "vehicle_id": VIN, "charger_type": "tesla",
        "dynamic_mode": "solar_surplus", "owner_mode": "manual_solar_surplus",
        "quick_control": True, "duration_minutes": 3,
        "expires_at": expiry.isoformat(), "_boost_start_deadline": expiry.timestamp(),
        "_user_initiated": True,
    }
    actions._dynamic_ev_state.clear()
    try:
        assert not await actions._action_start_ev_charging_dynamic(hass, entry, params)
        assert ev_ownership.get_ev_last_commands(hass, entry)[VIN]["reason"] == reason
        assert not ev_ownership.get_ev_quick_control_obligation(hass, entry, VIN)
        assert not hass.services.calls
    finally:
        actions._dynamic_ev_state.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry", "tessie"])
async def test_native_ack_is_journaled_without_physical_confirmation(monkeypatch, platform):
    hass = _native_hass(monkeypatch, platform)
    finished = _journal_mocks(monkeypatch)
    params = {"vehicle_vin": VIN}
    authority = lambda: True

    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        authority,
    )

    assert result.outcome == "accepted_unconfirmed"
    assert finished == ["accepted_unconfirmed"]
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": ENTITY})]
    assert "_tesla_transport_recovery_pending" not in params
    # The wrapper records provider acknowledgement only; the action layer owns
    # its existing physical Start readback requirement.


@pytest.mark.asyncio
async def test_tessie_completed_price_level_stop_reaches_native_service(monkeypatch):
    """Ticket #410: a tracked completed EV must not be stranded before Stop."""
    hass = _native_hass(monkeypatch, "tessie")
    hass.states._states["sensor.car_charging"] = _State("sensor.car_charging", "complete")
    finished = _journal_mocks(monkeypatch)
    monkeypatch.setattr(actions, "_get_ev_config", lambda _entry: {
        "ev_provider": actions.EV_PROVIDER_FLEET_API,
    })
    monkeypatch.setattr(actions, "_resolve_ble_prefix_for_vehicle", lambda *_args: "ble")
    monkeypatch.setattr(actions, "_tesla_transport_route", AsyncMock(return_value=None))
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *a, **kw: lambda: True)
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", AsyncMock(side_effect=lambda h, pattern, vin: (
        "sensor.car_charging" if pattern.startswith("sensor") else ENTITY
    )))
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *_args: True)
    monkeypatch.setattr(actions, "_wake_tesla_ev_for_command", AsyncMock(return_value=True))
    params = {"vehicle_vin": VIN, "stop_untracked": True}

    assert await actions._action_stop_ev_charging.__wrapped__(hass, _Entry(), params) is True
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": ENTITY})]
    assert finished == ["accepted_unconfirmed"]


@pytest.mark.parametrize("change", ["vin", "disabled", "provider"])
def test_tessie_native_binding_retains_identity_refusals(monkeypatch, change):
    hass = _native_hass(monkeypatch, "tessie")
    registration = hass.entity_registry.entities[ENTITY]
    if change == "vin":
        hass.device_registry.devices["tessie-device"].identifiers = {("tessie", "5YJ3E1EA7NF0000B2")}
    elif change == "disabled":
        registration.disabled_by = "user"
    else:
        registration.platform = "tesla_fleet"
    assert actions._tesla_native_binding(hass, ENTITY, VIN) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["authority", "deadline", "registry"])
async def test_native_submission_rechecks_scope_after_ticket_persistence(
    monkeypatch, mutation
):
    """A scope change while the durable ticket saves must block the HA write."""
    hass = _native_hass(monkeypatch, "tesla_fleet")
    finished = _journal_mocks(monkeypatch)
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    authority_state = {"allowed": True}
    params = {"vehicle_vin": VIN, "_boost_start_deadline": time.time() + 60}

    async def tracked(_hass, _entry, submit, *_args, **_kwargs):
        if mutation == "authority":
            authority_state["allowed"] = False
        elif mutation == "deadline":
            params["_boost_start_deadline"] = time.time() - 1
        else:
            hass.entity_registry.entities[ENTITY].unique_id = "rebound-provider"
        return await submit()

    monkeypatch.setattr(recovery, "async_track_submission", tracked)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        lambda: authority_state["allowed"],
    )

    assert result.outcome == "not_sent"
    assert finished == ["not_sent"]
    assert hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "domain", "service", "data"),
    (
        ("start", "switch", "turn_on", {"entity_id": ENTITY}),
        ("rate", "number", "set_value", {"entity_id": ENTITY, "value": 16}),
        ("limit", "number", "set_value", {"entity_id": ENTITY, "value": 90}),
    ),
)
@pytest.mark.parametrize(
    "deadline",
    [True, "expired-start-deadline", float("nan"), float("inf"), float("-inf")],
)
async def test_positive_native_write_rejects_invalid_deadline(
    monkeypatch, operation, domain, service, data, deadline
):
    entity_id = ENTITY if domain == "switch" else AMPS_ENTITY
    data = {**data, "entity_id": entity_id}
    hass = _native_hass(monkeypatch, "tesla_fleet", entity_id)
    finished = _journal_mocks(monkeypatch)
    params = {"vehicle_vin": VIN, "_boost_start_deadline": deadline}

    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, operation, entity_id, domain, service, data, lambda: True,
    )

    assert result.outcome == "not_sent"
    assert finished == []
    assert hass.services.calls == []



@pytest.mark.asyncio
async def test_native_start_rejects_elapsed_deadline(monkeypatch):
    hass = _native_hass(monkeypatch, "teslemetry")
    finished = _journal_mocks(monkeypatch)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN, "_boost_start_deadline": time.time() - 1},
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "not_sent"
    assert finished == []
    assert hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [pytest.param(0.0, id="elapsed"), "expired-start-deadline", True,
                                      float("nan"), float("inf"), float("-inf")])
async def test_native_stop_remains_available_after_old_start_deadline(monkeypatch, deadline):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    finished = _journal_mocks(monkeypatch)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN, "_boost_start_deadline": deadline},
        "stop",
        ENTITY,
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "accepted_unconfirmed"
    assert finished == ["accepted_unconfirmed"]
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": ENTITY})]


@pytest.mark.asyncio
async def test_native_write_is_fenced_when_journal_persist_does_not_admit(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    _journal_mocks(monkeypatch, ticket=None)
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")

    async def no_ticket(*_args, **_kwargs):
        return None

    monkeypatch.setattr(recovery, "async_begin_attempt", no_ticket)
    params = {"vehicle_vin": VIN}

    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )

    assert result.outcome == "not_sent"
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_owned_cleanup_stop_writes_once_when_attempt_persist_refused(monkeypatch):
    """A typed cleanup refusal permits one exact Stop and remains unknown."""
    hass = _native_hass(monkeypatch, "tesla_fleet")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    finished = []
    shared_tracker_calls = []

    async def refused(*_args, **_kwargs):
        raise recovery.RecoveryPersistenceRefused(
            "journal unavailable",
            vehicle_id=VIN,
            route={
                "kind": "ha",
                "entity_id": ENTITY,
                "vehicle_id": VIN,
                "cleanup_scope_valid": True,
                "cleanup_obligation_id": "obligation-1",
            },
        )

    async def finish(*_args, **_kwargs):
        finished.append(True)
        return True

    async def shared_tracker(
        _hass, _entry, refusal, submit, *, vehicle_id, generation
    ):
        shared_tracker_calls.append((refusal, vehicle_id, generation))
        return await submit()

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)
    monkeypatch.setattr(recovery, "async_track_cleanup_submission", shared_tracker)
    params = {
        "vehicle_vin": VIN,
        "_ev_cleanup_only": True,
        "_quick_control_obligation_id": "obligation-1",
    }

    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "stop",
        ENTITY,
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "sent_unknown"
    assert finished == []
    assert hass.services.calls == [("switch", "turn_off", {"entity_id": ENTITY})]
    assert len(shared_tracker_calls) == 1
    assert isinstance(shared_tracker_calls[0][0], recovery.RecoveryPersistenceRefused)
    assert params["_tesla_cleanup_fallback_consumed"] is True


@pytest.mark.asyncio
async def test_native_cleanup_handles_core_persistence_refusal_exception(monkeypatch):
    """The production core raises its typed refusal, rather than a ticket."""
    hass = _native_hass(monkeypatch, "tesla_fleet")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    calls = []
    shared_tracker_calls = []

    async def refused(*_args, **kwargs):
        assert kwargs["allow_persist_refused_cleanup"] is True
        assert kwargs["cleanup_obligation_token"] == "obligation-exception"
        raise recovery.RecoveryPersistenceRefused(
            "journal unavailable",
            vehicle_id=VIN,
            route={
                "kind": "ha",
                "entity_id": ENTITY,
                "cleanup_obligation_id": "obligation-exception",
            },
        )

    async def service_call(*_args, **_kwargs):
        calls.append(True)

    async def shared_tracker(
        _hass, _entry, refusal, submit, *, vehicle_id, generation
    ):
        shared_tracker_calls.append((refusal, vehicle_id, generation))
        return await submit()

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    monkeypatch.setattr(
        recovery, "async_track_cleanup_submission", shared_tracker,
        raising=False,
    )
    monkeypatch.setattr(hass.services, "async_call", service_call)
    params = {
        "vehicle_vin": VIN,
        "_ev_cleanup_only": True,
        "_quick_control_obligation_id": "obligation-exception",
    }

    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "stop",
        ENTITY,
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "sent_unknown"
    assert calls == [True]
    assert len(shared_tracker_calls) == 1
    assert shared_tracker_calls[0][1:] == (VIN, 0)
    assert params["_tesla_cleanup_fallback_consumed"] is True


@pytest.mark.asyncio
async def test_legacy_ble_cleanup_stop_writes_once_when_attempt_persist_refused(monkeypatch):
    hass = _Hass(
        [_State("switch.ble_charge", "on")],
        {
            "switch.ble_charge": SimpleNamespace(
                entity_id="switch.ble_charge",
                platform="esphome",
                config_entry_id="ble-entry",
                device_id="ble-device",
                unique_id="ble-charge",
                disabled_by=None,
            )
        },
        {
            "ble-device": SimpleNamespace(
                id="ble-device",
                identifiers={("esphome", "ble-device")},
                config_entries={"ble-entry"},
            )
        },
    )
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(actions.er, "async_get", lambda _hass: hass.entity_registry)
    monkeypatch.setattr(actions.dr, "async_get", lambda _hass: hass.device_registry)
    shared_tracker_calls = []

    async def refused(*_args, **_kwargs):
        raise recovery.RecoveryPersistenceRefused(
            "journal unavailable",
            vehicle_id=VIN,
            route={
                "kind": "ble",
                "prefix": "ble",
                "entity_id": "switch.ble_charge",
                "vehicle_id": VIN,
                "cleanup_scope_valid": True,
                "cleanup_obligation_id": "obligation-ble",
            },
        )

    async def shared_tracker(
        _hass, _entry, refusal, submit, *, vehicle_id, generation
    ):
        shared_tracker_calls.append((refusal, vehicle_id, generation))
        return await submit()

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    monkeypatch.setattr(recovery, "async_track_cleanup_submission", shared_tracker)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )
    params = {
        "vehicle_vin": VIN,
        "_ev_cleanup_only": True,
        "_quick_control_obligation_id": "obligation-ble",
    }

    result = await actions._run_ble_transport_command(
        hass,
        _Entry(),
        params,
        "ble",
        "stop",
        lambda: actions._tesla_call_service(
            hass,
            "switch",
            "turn_off",
            {"entity_id": "switch.ble_charge"},
        ),
    )

    assert result == (None, False)
    assert hass.services.calls == [
        ("switch", "turn_off", {"entity_id": "switch.ble_charge"})
    ]
    assert len(shared_tracker_calls) == 1
    assert isinstance(shared_tracker_calls[0][0], recovery.RecoveryPersistenceRefused)
    assert params["_tesla_cleanup_fallback_consumed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params,route",
    [
        (
            {
                "vehicle_vin": VIN,
                "_ev_cleanup_only": True,
                "_quick_control_obligation_id": "wrong-token",
            },
            {"cleanup_persistence_refused": True, "cleanup_obligation_id": "right-token"},
        ),
        (
            {"vehicle_vin": VIN, "_ev_cleanup_only": True},
            {"cleanup_persistence_refused": True},
        ),
    ],
    ids=["wrong-obligation", "missing-exact-scope"],
)
async def test_cleanup_persistence_refusal_never_writes_without_matching_scope(
    monkeypatch, params, route
):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(cleanup_persistence_refused=True, route=route)

    async def refused(*_args, **_kwargs):
        return ticket

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "stop",
        ENTITY,
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "not_sent"
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_cleanup_persistence_refusal_rejects_native_rebind_before_write(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(
        cleanup_persistence_refused=True,
        route={
            "cleanup_persistence_refused": True,
            "cleanup_obligation_id": "obligation-1",
        },
    )

    async def refused(*_args, **_kwargs):
        hass.entity_registry.entities[ENTITY].unique_id = "replacement-provider"
        return ticket

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        {
            "vehicle_vin": VIN,
            "_ev_cleanup_only": True,
            "_quick_control_obligation_id": "obligation-1",
        },
        "stop",
        ENTITY,
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "not_sent"
    assert hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,service_data", [
    ("start", {"entity_id": ENTITY}),
    ("rate", {"entity_id": AMPS_ENTITY, "value": 8}),
    ("limit", {"entity_id": LIMIT_ENTITY, "value": 80}),
])
async def test_cleanup_persistence_refusal_never_falls_back_to_positive_write(
    monkeypatch, operation, service_data
):
    hass = _native_hass(monkeypatch, "tesla_fleet", service_data["entity_id"])
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(
        cleanup_persistence_refused=True,
        route={"cleanup_persistence_refused": True, "cleanup_scope_valid": True},
    )

    async def refused(*_args, **_kwargs):
        return ticket

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN, "_ev_cleanup_only": True},
        operation,
        service_data["entity_id"],
        "switch" if operation == "start" else "number",
        "turn_on" if operation == "start" else "set_value",
        service_data,
        lambda: True,
    )

    assert result.outcome == "not_sent"
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_native_write_requires_device_identifier_for_requested_vin(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    device = hass.device_registry.devices[f"tesla_fleet-device"]
    device.identifiers = {("tesla_fleet", "5YJ3E1EA7NF0000B2")}
    finished = _journal_mocks(monkeypatch)

    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )

    assert result.outcome == "not_sent"
    assert finished == []
    assert hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["binding", "deadline"])
async def test_native_write_rechecks_binding_and_deadline_after_persist(monkeypatch, change):
    hass = _native_hass(monkeypatch, "teslemetry", AMPS_ENTITY)
    params = {"vehicle_vin": VIN, "_boost_start_deadline": time.time() + 100.0}

    def mutate_scope():
        if change == "binding":
            hass.entity_registry.entities[AMPS_ENTITY].device_id = "replacement-device"
        else:
            params["_boost_start_deadline"] = time.time() + 200.0

    finished = _journal_mocks(monkeypatch, on_begin=mutate_scope)
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "rate", AMPS_ENTITY, "number", "set_value",
        {"entity_id": AMPS_ENTITY, "value": 16}, lambda: True,
    )

    assert result.outcome == "not_sent"
    assert finished == ["not_sent"]
    assert hass.services.calls == []



@pytest.mark.asyncio
async def test_native_timeout_keeps_durable_unknown_and_blocks_retry(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    finished = _journal_mocks(monkeypatch)

    async def timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError("service timed out")

    hass.services.async_call = timeout
    params = {"vehicle_vin": VIN}
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "rate", AMPS_ENTITY, "number", "set_value",
        {"entity_id": AMPS_ENTITY, "value": 30}, lambda: True,
    )

    assert result.outcome == "sent_unknown"
    assert finished == ["sent_unknown"]
    assert params["_tesla_transport_recovery_pending"] is True
    assert hass.services.calls == []



@pytest.mark.asyncio
async def test_native_cancellation_propagates_after_unknown_is_durable(monkeypatch):
    hass = _native_hass(monkeypatch, "teslemetry")
    finished = _journal_mocks(monkeypatch)

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError

    hass.services.async_call = cancelled
    params = {"vehicle_vin": VIN}
    with pytest.raises(asyncio.CancelledError):
        await actions._run_native_tesla_service_command(
            hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
            {"entity_id": ENTITY}, lambda: True,
        )

    assert finished == ["sent_unknown"]
    assert params["_tesla_transport_recovery_pending"] is True
    assert params["_tesla_allow_plan_recovery"] is True


@pytest.mark.asyncio
async def test_native_cancellation_during_accepted_finish_keeps_plan_recovery_marker(monkeypatch):
    hass = _native_hass(monkeypatch, "teslemetry")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(attempt_id="accepted-finish-cancel")
    finish_started = asyncio.Event()
    finish_release = asyncio.Event()
    finished = []

    async def begin(*_args, **_kwargs):
        return ticket

    async def finish(_ticket, outcome):
        finish_started.set()
        await finish_release.wait()
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)
    params = {"vehicle_vin": VIN}
    task = asyncio.create_task(actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    ))
    await finish_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert params["_tesla_transport_recovery_pending"] is True
    assert params["_tesla_allow_plan_recovery"] is True

    finish_release.set()
    await asyncio.sleep(0)
    assert finished == ["accepted_unconfirmed"]


@pytest.mark.asyncio
async def test_native_range_error_is_unknown_and_never_retried(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    finished = _journal_mocks(monkeypatch)
    calls = []

    async def rejected(domain, service, data, **kwargs):
        calls.append((domain, service, data, kwargs))
        raise RuntimeError("outside valid range")

    hass.services.async_call = rejected
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "rate", AMPS_ENTITY, "number", "set_value",
        {"entity_id": AMPS_ENTITY, "value": 30}, lambda: True,
    )

    assert result.outcome == "sent_unknown"
    assert finished == ["sent_unknown"]
    assert len(calls) == 1



@pytest.mark.asyncio
async def test_native_unknown_records_manual_key_diagnostic_without_reclassifying(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    finished = _journal_mocks(monkeypatch)

    async def rejected(*_args, **_kwargs):
        raise RuntimeError("Vehicle did not recognize the key used to authorize command")

    hass.services.async_call = rejected
    failure = {}
    params = {"vehicle_vin": VIN, "_manual_command_failure": failure}
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )

    assert result.outcome == "sent_unknown"
    assert finished == ["sent_unknown"]
    assert params["_tesla_transport_recovery_pending"] is True
    assert failure == {"provider": "tesla", "code": "key_not_paired"}


@pytest.mark.asyncio
async def test_fleet_stop_does_not_require_metadata_refresh_and_preserves_unknown_hazard(monkeypatch):
    """Registered Fleet Stop reaches its owner even when refresh would fail."""
    hass = _native_hass(monkeypatch, "tesla_fleet")
    finished = _journal_mocks(monkeypatch)
    entry = _Entry()
    params = {
        "charger_type": "tesla",
        "vehicle_vin": VIN,
        "_force_tesla_stop_request": True,
        "_boost_start_deadline": time.time() - 1,
    }

    async def route(*_args, **_kwargs):
        return None

    async def entity(*_args, **_kwargs):
        return ENTITY

    refresh_calls = []

    async def refresh_failure(*_args, **_kwargs):
        refresh_calls.append(True)
        raise AssertionError("Fleet metadata refresh must not gate cleanup Stop")

    async def timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError("Fleet stop outcome is unknown")

    monkeypatch.setattr(actions, "_get_ev_config", lambda _entry: {
        "ev_provider": actions.EV_PROVIDER_FLEET_API,
    })
    monkeypatch.setattr(actions, "_resolve_ble_prefix_for_vehicle", lambda *_args: "ble")
    monkeypatch.setattr(actions, "_tesla_transport_route", route)
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *args, **kwargs: lambda: True)
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", entity)
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *_args: True)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )
    monkeypatch.setattr(actions, "_tesla_ha_command_entity", entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev_for_command", refresh_failure)
    hass.services.async_call = timeout
    result = await actions._action_stop_ev_charging.__wrapped__(hass, entry, params)

    assert result is None
    assert refresh_calls == []
    assert finished == ["sent_unknown"]
    assert params["_tesla_transport_recovery_pending"] is True


@pytest.mark.asyncio
async def test_native_structured_range_validation_is_the_only_retryable_error(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    finished = _journal_mocks(monkeypatch)
    import homeassistant.exceptions as exceptions

    class ServiceValidationError(Exception):
        translation_domain = "number"
        translation_key = "out_of_range"
        translation_placeholders = {
            "entity_id": AMPS_ENTITY,
            "value": "30",
            "min_value": "1",
            "max_value": "16",
        }

    monkeypatch.setattr(exceptions, "ServiceValidationError", ServiceValidationError, raising=False)

    async def rejected(*_args, **_kwargs):
        raise ServiceValidationError("structured range rejection")

    hass.services.async_call = rejected
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "rate", AMPS_ENTITY, "number", "set_value",
        {"entity_id": AMPS_ENTITY, "value": 30}, lambda: True,
    )

    assert result.outcome == "not_sent"
    assert result.retryable_range is True
    assert finished == ["not_sent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry", "tessie"])
async def test_native_unknown_survives_reload_and_stop_without_another_start(monkeypatch, platform):
    """Exercise the real recovery journal, replacement runtime and service count."""
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    persisted = {}

    async def persist(_hass, _entry, runtime):
        persisted.clear()
        persisted.update(deepcopy(runtime["ev_transport_recovery"]))
        return True

    monkeypatch.setattr(recovery, "_persist", persist)
    monkeypatch.setattr(recovery, "_registry", lambda h: h.entity_registry)
    hass = _native_hass(monkeypatch, platform)
    submitted = []

    async def lost_response(domain, service, data, **_kwargs):
        submitted.append((domain, service, data))
        raise TimeoutError("response lost after submission")

    hass.services.async_call = lost_response
    params = {"vehicle_vin": VIN}
    receipt = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )
    assert receipt.outcome == "sent_unknown"
    assert len(submitted) == 1
    assert len(persisted["vehicles"][VIN]["attempts"]) == 1

    restored = _native_hass(monkeypatch, platform)
    restored.data["power_sync"]["entry-1"]["ev_transport_recovery"] = deepcopy(persisted)
    assert recovery.get_vehicle_route(restored, _Entry(), VIN)["kind"] == "blocked"
    from power_sync import tesla_ev_observation as observation

    selected_sources = []

    async def read_exact(_hass, _vin, *, preferred_entity_id, timeout):
        selected_sources.append(preferred_entity_id)
        return None

    monkeypatch.setattr(observation, "async_read_tesla_vehicle", read_exact)
    await actions._fresh_tesla_command_observation(restored, _Entry(), VIN, {})
    assert selected_sources == [ENTITY]
    receipt = await actions._run_native_tesla_service_command(
        restored, _Entry(), {"vehicle_vin": VIN}, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )
    assert receipt.outcome == "not_sent"
    assert restored.services.calls == []

    receipt = await actions._run_native_tesla_service_command(
        restored, _Entry(), {"vehicle_vin": VIN, "_boost_start_deadline": float("nan")},
        "stop", ENTITY, "switch", "turn_off", {"entity_id": ENTITY}, lambda: True,
    )
    assert receipt.outcome == "accepted_unconfirmed"
    assert restored.services.calls == [("switch", "turn_off", {"entity_id": ENTITY})]
    assert len(persisted["vehicles"][VIN]["attempts"]) == 1
    assert recovery.get_vehicle_route(restored, _Entry(), VIN)["kind"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_transition", ["replaced", "released", "reload"])
async def test_native_unknown_rate_allows_exact_stop_after_lease_transition(
    monkeypatch, lease_transition,
):
    """A new manual session cannot strand Stop behind an older unknown Rate."""
    import importlib

    from power_sync.automations import ev_ownership

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    persisted = {}

    async def persist(_hass, _entry, runtime):
        persisted.clear()
        persisted.update(deepcopy(runtime[recovery.RECOVERY_KEY]))
        return True

    monkeypatch.setattr(recovery, "_persist", persist)
    monkeypatch.setattr(recovery, "_registry", lambda h: h.entity_registry)
    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    hass.entity_registry.entities[AMPS_ENTITY] = SimpleNamespace(
        entity_id=AMPS_ENTITY, platform="tesla_fleet",
        config_entry_id="tesla_fleet-entry", device_id="tesla_fleet-device",
        unique_id=f"{VIN}-amps", disabled_by=None,
    )
    hass.states._states[AMPS_ENTITY] = _State(AMPS_ENTITY, "16")
    ev_ownership.claim_ev_ownership(
        hass, entry, VIN, owner_mode="solar_surplus", session_id="old-session",
    )
    params = {"vehicle_vin": VIN, "charger_type": "tesla"}

    async def native(operation, entity, domain, service, **values):
        return await actions._run_native_tesla_service_command(
            hass, entry, dict(params), operation, entity, domain, service,
            {"entity_id": entity, **values},
            actions._tesla_transport_authority(
                hass, entry, params, operation=operation, cleanup=operation == "stop",
            ),
        )

    assert (await native("start", ENTITY, "switch", "turn_on")).outcome == "accepted_unconfirmed"
    original_call = hass.services.async_call

    async def lost_rate_response(domain, service, data, **kwargs):
        await original_call(domain, service, data, **kwargs)
        raise RuntimeError("native current response unavailable")

    hass.services.async_call = lost_rate_response
    assert (await native("rate", AMPS_ENTITY, "number", "set_value", value=17)).outcome == "sent_unknown"
    hass.services.async_call = original_call
    assert (await native("stop", ENTITY, "switch", "turn_off")).outcome == "accepted_unconfirmed"
    original_attempts = deepcopy(persisted["vehicles"][VIN]["attempts"])
    assert len(original_attempts) == 1
    unknown = next(iter(original_attempts.values()))
    assert unknown["operation"] == "rate" and unknown["target"] is None
    assert unknown["route"]["command_request"] == {"operation": "rate", "value": 17, "unit": "A"}
    assert unknown["route"]["session_anchor"]["entity_id"] == ENTITY

    if lease_transition == "released":
        ev_ownership.release_ev_ownership(hass, entry, VIN, reason="Manual stop")
    else:
        ev_ownership.claim_ev_ownership(
            hass, entry, VIN, owner_mode="manual_solar_surplus", session_id="new-session",
        )
    if lease_transition == "reload":
        old_runtime = hass.data["power_sync"][entry.entry_id]
        hass.data["power_sync"][entry.entry_id] = {
            "automation_store": old_runtime["automation_store"],
            "ev_ownership": deepcopy(old_runtime["ev_ownership"]),
            recovery.RECOVERY_KEY: deepcopy(persisted),
        }

    calls_before = len(hass.services.calls)
    assert recovery.get_vehicle_route(hass, entry, VIN)["kind"] == "blocked"
    assert (await native("start", ENTITY, "switch", "turn_on")).outcome == "not_sent"
    assert len(hass.services.calls) == calls_before
    result = await actions._action_stop_ev_charging.__wrapped__(hass, entry, {
        **params, "_force_tesla_stop_request": True, "_user_initiated": True,
        "_ev_cleanup_only": True,
    })
    assert result is True
    assert hass.services.calls[calls_before:] == [("switch", "turn_off", {"entity_id": ENTITY})]
    assert persisted["vehicles"][VIN]["attempts"] == original_attempts
    assert persisted["vehicles"][VIN]["route"]["session_scope"]["session_id"] == "old-session"
    assert (await native("start", ENTITY, "switch", "turn_on")).outcome == "not_sent"
    assert recovery.get_vehicle_route(hass, entry, "5YJ3E1EA7NF0000B2") is None

    # Finite controls must explain the retained hold without claiming that
    # this unchanged producer was rebound, and must not write a Start.
    from datetime import datetime, timedelta, timezone
    actions._dynamic_ev_state.clear()
    before = len(hass.services.calls)
    expiry = datetime.now(timezone.utc) + timedelta(minutes=3)
    finite_params = {
        **params, "vehicle_id": VIN, "dynamic_mode": "solar_surplus",
        "owner_mode": "manual_solar_surplus", "quick_control": True,
        "duration_minutes": 3, "expires_at": expiry.isoformat(),
        "_boost_start_deadline": expiry.timestamp(), "_user_initiated": True,
    }
    try:
        assert not await actions._action_start_ev_charging_dynamic(hass, entry, finite_params)
        assert ev_ownership.get_ev_last_commands(hass, entry)[VIN]["reason"] == (
            "Tesla finite EV control held by unresolved native command outcomes; recovery required"
        )
        assert not ev_ownership.get_ev_quick_control_obligation(hass, entry, VIN)
        assert hass.services.calls[before:] == []
        assert persisted["vehicles"][VIN]["attempts"] == original_attempts
    finally:
        actions._dynamic_ev_state.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_runtime", [False, True])
async def test_expired_native_finite_session_rebinds_exact_cleanup_stop_only(
    monkeypatch, replace_runtime,
):
    """Expiry revokes positive ownership but still submits its native Stop."""
    import importlib

    from power_sync.automations import ev_ownership

    entry = SimpleNamespace(
        entry_id="entry-1",
        data={},
        options={"ev_provider": actions.EV_PROVIDER_FLEET_API},
    )
    hass = _native_hass(monkeypatch, "tesla_fleet")
    from power_sync import teslemetry_solar

    solar_store = SimpleNamespace(
        async_load=AsyncMock(return_value={"vehicles": {}, "requests": {}}),
        async_save=AsyncMock(return_value=None),
    )
    hass.data["power_sync_native_solar"] = {
        "entry-1": teslemetry_solar.SolarOwnership(solar_store),
    }
    hass.data["power_sync_native_solar"]["entry-1"].holds = {}
    hass.entity_registry.entities[AMPS_ENTITY] = SimpleNamespace(
        entity_id=AMPS_ENTITY,
        platform="tesla_fleet",
        config_entry_id="tesla_fleet-entry",
        device_id="tesla_fleet-device",
        unique_id=f"{VIN}-amps",
        disabled_by=None,
    )
    hass.states._states[AMPS_ENTITY] = _State(AMPS_ENTITY, "10")
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "_registry", lambda h: h.entity_registry)

    async def persist(_hass, _entry, _runtime):
        return True

    monkeypatch.setattr(recovery, "_persist", persist)
    owner = ev_ownership.claim_ev_ownership(
        hass,
        entry,
        VIN,
        owner_mode="manual",
        session_id="finite-session",
    )
    assert owner["owner"] == "powersync"

    # The real native Start and Rate establish the durable session route.
    start = await actions._run_native_tesla_service_command(
        hass,
        entry,
        {"vehicle_vin": VIN},
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        actions._tesla_transport_authority(
            hass, entry, {"vehicle_vin": VIN}, operation="start"
        ),
    )
    assert start.outcome == "accepted_unconfirmed"
    rate = await actions._run_native_tesla_service_command(
        hass,
        entry,
        {"vehicle_vin": VIN},
        "rate",
        AMPS_ENTITY,
        "number",
        "set_value",
        {"entity_id": AMPS_ENTITY, "value": 10},
        actions._tesla_transport_authority(
            hass, entry, {"vehicle_vin": VIN}, operation="rate"
        ),
    )
    assert rate.outcome == "accepted_unconfirmed"
    assert recovery.get_vehicle_route(hass, entry, VIN)["kind"] == "ha"

    binding = await actions._tesla_quick_control_binding(
        hass, entry, {"vehicle_vin": VIN}, VIN
    )
    token = "finite-obligation"
    hass.data["power_sync"][entry.entry_id][
        ev_ownership.EV_QUICK_OBLIGATIONS_KEY
    ] = {
        VIN: {
            "obligation_id": token,
            "expires_at": "2099-01-01T00:00:00+00:00",
            "params": {
                "vehicle_vin": VIN,
                "charger_type": "tesla",
                "_quick_control_obligation_id": token,
                "_quick_control_binding": binding,
            },
        }
    }
    ev_ownership.release_ev_ownership(
        hass, entry, VIN, reason="finite deadline elapsed", command="stop", success=False
    )
    blocked = recovery.get_vehicle_route(hass, entry, VIN)
    assert blocked["kind"] == "blocked"
    assert blocked["reason"] == "route_identity_changed"

    if replace_runtime:
        replacement_holder = {}

        async def replace_during_save(_hass, _entry, _runtime):
            old_runtime = hass.data["power_sync"][entry.entry_id]
            replacement_runtime = dict(old_runtime)
            for key in (
                "ev_ownership",
                "ev_last_command",
                "ev_boost_sessions",
                ev_ownership.EV_QUICK_OBLIGATIONS_KEY,
                "ev_transport_recovery",
            ):
                if key in old_runtime:
                    replacement_runtime[key] = deepcopy(old_runtime[key])
            hass.data["power_sync"][entry.entry_id] = replacement_runtime
            replacement_holder["runtime"] = replacement_runtime
            return True

        monkeypatch.setattr(recovery, "_persist", replace_during_save)
        cleanup_params = {
            "vehicle_vin": VIN,
            "vehicle_id": VIN,
            "_ev_cleanup_only": True,
            "_quick_control_obligation_id": token,
        }
        assert await actions._tesla_prepare_native_cleanup_handoff(
            hass, entry, cleanup_params
        ) is False
        replacement_runtime = replacement_holder["runtime"]
        assert hass.data["power_sync"][entry.entry_id] is replacement_runtime
        replacement_lease = ev_ownership.get_ev_ownership(
            hass, entry, VIN
        )[1]
        assert replacement_lease is not None
        assert replacement_lease.get("owner_mode") == "tesla_cleanup"
        assert hass.services.calls == [
            ("switch", "turn_on", {"entity_id": ENTITY}),
            ("number", "set_value", {"entity_id": AMPS_ENTITY, "value": 10}),
        ]
        return

    stop_started = asyncio.Event()
    release_stop = asyncio.Event()
    original_call = hass.services.async_call

    async def service_call(domain, service, data, **kwargs):
        if service == "turn_off":
            stop_started.set()
            await release_stop.wait()
        return await original_call(domain, service, data, **kwargs)

    hass.services.async_call = service_call
    stop_params = {
        "charger_type": "tesla",
        "vehicle_vin": VIN,
        "vehicle_id": VIN,
        "stop_charging": True,
        "stop_untracked": True,
        "manual_stop": True,
        "_force_tesla_stop_request": True,
        "_ev_cleanup_only": True,
        "_quick_control_obligation_id": token,
    }
    stop_task = asyncio.create_task(
        actions.execute_actions(
            hass,
            entry,
            [{
                "action_type": "stop_ev_charging_dynamic",
                "parameters": stop_params,
            }],
        )
    )
    await asyncio.wait_for(stop_started.wait(), 2)
    _cleanup_lease_id, cleanup_lease = ev_ownership.get_ev_ownership(
        hass, entry, VIN
    )
    assert cleanup_lease is not None
    assert cleanup_lease.get("owner_mode") == "tesla_cleanup"

    # The temporary route is persisted stop-only while the provider call is
    # in flight.  A positive request must still be denied without a service.
    positive_call_count = len(
        [call for call in hass.services.calls if call[1] == "turn_on"]
    )
    positive = await actions._run_native_tesla_service_command(
        hass,
        entry,
        {"vehicle_vin": VIN},
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        actions._tesla_transport_authority(
            hass, entry, {"vehicle_vin": VIN}, operation="start"
        ),
    )
    assert positive.outcome == "not_sent"
    assert len(
        [call for call in hass.services.calls if call[1] == "turn_on"]
    ) == positive_call_count

    release_stop.set()
    assert await stop_task is True
    assert hass.services.calls[-1] == (
        "switch",
        "turn_off",
        {"entity_id": ENTITY},
    )
    assert ev_ownership.get_ev_ownership(hass, entry, VIN)[1] is None


@pytest.mark.asyncio
async def test_native_service_domain_mismatch_cannot_reach_a_provider(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    finished = _journal_mocks(monkeypatch)
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "start", AMPS_ENTITY, "switch", "turn_on",
        {"entity_id": AMPS_ENTITY}, lambda: True,
    )
    assert result.outcome == "not_sent"
    assert finished == []
    assert hass.services.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry", "tessie"])
async def test_terminal_native_command_does_not_pin_future_commands_away_from_ble(monkeypatch, platform):
    """A completed fallback must return the next request to configured routing."""
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    persisted = {}

    async def persist(_hass, _entry, runtime):
        persisted.clear()
        persisted.update(deepcopy(runtime["ev_transport_recovery"]))
        return True

    monkeypatch.setattr(recovery, "_persist", persist)
    monkeypatch.setattr(recovery, "_registry", lambda h: h.entity_registry)
    hass = _native_hass(monkeypatch, platform)
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )
    assert result.outcome == "accepted_unconfirmed"
    assert hass.services.calls == [("switch", "turn_on", {"entity_id": ENTITY})]
    assert persisted["vehicles"][VIN]["attempts"] == {}
    assert recovery.get_vehicle_route(hass, _Entry(), VIN) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["device_vin", "deadline"])
async def test_native_final_authority_await_cannot_rebind_or_outlive_command(monkeypatch, changed):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    finished = _journal_mocks(monkeypatch)
    params = {"vehicle_vin": VIN, "_boost_start_deadline": time.time() + 60}
    checks = 0

    async def authority():
        nonlocal checks
        checks += 1
        if checks == 2:
            await asyncio.sleep(0)
            if changed == "device_vin":
                hass.device_registry.devices["tesla_fleet-device"].identifiers = {
                    ("tesla_fleet", "5YJ3E1EA7NF0000B2")
                }
            else:
                params["_boost_start_deadline"] = time.time() - 1
        return True

    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, authority,
    )
    assert result.outcome == "not_sent"
    assert finished == ["not_sent"]
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_native_unknown_keeps_exact_command_entity_for_readback(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    _journal_mocks(monkeypatch)

    async def lost_response(*_args, **_kwargs):
        raise TimeoutError

    hass.services.async_call = lost_response
    params = {"vehicle_vin": VIN}
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), params, "start", ENTITY, "switch", "turn_on",
        {"entity_id": ENTITY}, lambda: True,
    )
    assert result.outcome == "sent_unknown"
    assert params["_tesla_command_entity_id"] == ENTITY


@pytest.mark.asyncio
async def test_native_start_records_mutation_binding_for_session_handoff(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    _journal_mocks(monkeypatch)
    params = {"vehicle_vin": VIN}

    result = await actions._run_native_tesla_service_command(
        hass,
        _Entry(),
        params,
        "start",
        ENTITY,
        "switch",
        "turn_on",
        {"entity_id": ENTITY},
        lambda: True,
    )

    assert result.outcome == "accepted_unconfirmed"
    assert params["_tesla_native_command_identity"] == {
        "entity_id": ENTITY,
        "platform": "tesla_fleet",
        "config_entry_id": "tesla_fleet-entry",
        "device_id": "tesla_fleet-device",
        "unique_id": f"{VIN}-charge",
        "disabled_by": None,
    }


@pytest.mark.asyncio
async def test_native_dispatch_keeps_the_value_admitted_before_journal_await(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    data = {"entity_id": AMPS_ENTITY, "value": 8}
    finished = _journal_mocks(monkeypatch, on_begin=lambda: data.update(value=80))
    result = await actions._run_native_tesla_service_command(
        hass, _Entry(), {"vehicle_vin": VIN}, "rate", AMPS_ENTITY, "number", "set_value",
        data, lambda: True,
    )
    assert result.outcome == "accepted_unconfirmed"
    assert finished == ["accepted_unconfirmed"]
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": AMPS_ENTITY, "value": 8})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry", "tessie"])
async def test_native_start_anchor_routes_initial_rate_without_ble(monkeypatch, platform):
    """The transient Start binding wins before dynamic ownership is claimed."""
    import importlib

    hass = _native_hass(monkeypatch, platform)
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    params = {
        "vehicle_vin": VIN,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": _native_identity(hass),
    }
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: None)
    monkeypatch.setattr(
        actions,
        "_tesla_native_anchor_entity",
        lambda _hass, _params, operation: AMPS_ENTITY
        if operation == "rate"
        else None,
    )

    route = await actions._tesla_transport_route(hass, _Entry(), params, "rate")
    assert route == {"kind": "ha", "entity_id": ENTITY}
    assert await actions._tesla_ha_command_entity(
        hass, _Entry(), params, "rate"
    ) == AMPS_ENTITY


@pytest.mark.asyncio
async def test_native_start_anchor_rebind_fails_closed_before_ble_fallback(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: None)
    monkeypatch.setattr(actions, "_tesla_native_anchor_entity", lambda *_args: None)
    params = {
        "vehicle_vin": VIN,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": {
            **_native_identity(hass),
            "device_id": "replaced-device",
        },
    }

    assert await actions._tesla_transport_route(
        hass, _Entry(), params, "rate"
    ) == {"kind": "ha"}
    assert await actions._tesla_ha_command_entity(
        hass, _Entry(), params, "rate"
    ) is None


@pytest.mark.asyncio
async def test_native_start_anchor_prevents_ble_branch_for_initial_rate(monkeypatch):
    hass = _native_hass(monkeypatch, "tesla_fleet", AMPS_ENTITY)
    hass.entity_registry.entities[ENTITY] = SimpleNamespace(
        entity_id=ENTITY,
        platform="tesla_fleet",
        config_entry_id="tesla_fleet-entry",
        device_id="tesla_fleet-device",
        unique_id=f"{VIN}-charge",
        disabled_by=None,
    )
    hass.states._states[ENTITY] = _State(ENTITY, "off")
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: None)
    monkeypatch.setattr(
        actions,
        "_tesla_native_anchor_entity",
        lambda _hass, _params, operation: AMPS_ENTITY
        if operation == "rate"
        else None,
    )
    monkeypatch.setattr(actions, "_get_ev_config", lambda _entry: {
        "ev_provider": actions.EV_PROVIDER_BOTH,
    })
    monkeypatch.setattr(actions, "_resolve_ble_prefix_for_vehicle", lambda *_args: "ble")
    monkeypatch.setattr(
        actions,
        "_is_ble_available",
        lambda *_args: (_ for _ in ()).throw(AssertionError("BLE branch reached")),
    )
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *_args: True)
    async def native_entity(*_args, **_kwargs):
        return AMPS_ENTITY

    monkeypatch.setattr(actions, "_tesla_ha_command_entity", native_entity)
    monkeypatch.setattr(actions, "_wake_tesla_ev_for_command", lambda *_args: _async_true())
    monkeypatch.setattr(actions, "prepare_ev_submission", _async_true)

    async def accepted(*_args, **_kwargs):
        return SimpleNamespace(outcome="accepted_unconfirmed", error=None)

    monkeypatch.setattr(actions, "_run_native_tesla_service_command", accepted)
    params = {
        "charger_type": "tesla",
        "vehicle_vin": VIN,
        "amps": 16,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": _native_identity(hass, ENTITY),
    }
    assert await actions._action_set_ev_charging_amps.__wrapped__(
        hass, _Entry(), params
    ) is True


@pytest.mark.asyncio
async def test_stock_recovery_defers_old_positive_and_allows_fresh_route_retry(monkeypatch):
    """A successful stock reset cannot reuse the old positive authority."""
    import importlib

    hass = _native_hass(monkeypatch, "tesla_fleet")
    runtime = hass.data["power_sync"]["entry-1"]
    runtime["_tesla_ev_recovery_sidecar_loaded"] = True
    entry = _Entry()
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    stock = importlib.import_module("power_sync.tesla_ev_stock_recovery")
    routes = iter([
        {"kind": "blocked", "vehicle_id": VIN, "reason": "ble_recovery"},
        None,  # Route re-read after recovery; current request still defers.
        None,  # A fresh controller request can select the new route.
    ])
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: next(routes))
    monkeypatch.setattr(recovery, "pending_ble_attempt", lambda *_args: SimpleNamespace(
        attempt_id="attempt-1"
    ))
    monkeypatch.setattr(recovery, "async_isolate_ble", AsyncMock(return_value=False))
    monkeypatch.setattr(recovery, "recovery_sidecar_snapshot", lambda *_args: {"incidents": {}})
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *_args, **_kwargs: lambda: True)
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr(stock, "async_recover_vehicle_transport", recover)

    params = {"vehicle_vin": VIN}
    first = await actions._tesla_transport_route(hass, entry, params, "rate")

    assert first["kind"] == "blocked"
    assert first["reason"] == "ble_recovery"
    assert params["_tesla_transport_recovery_pending"] is True
    assert params["_tesla_stock_recovery_attempted"] is True
    recover.assert_awaited_once_with(hass, entry, VIN, incident_id=None)
    assert hass.services.calls == []

    fresh_params = {"vehicle_vin": VIN}
    assert await actions._tesla_transport_route(hass, entry, fresh_params, "rate") is None
    assert recover.await_count == 1


@pytest.mark.asyncio
async def test_stock_recovery_failure_keeps_sidecar_incident_blocked_without_ledger(monkeypatch):
    """A sidecar-only incident remains blocked when reset qualification fails."""
    import importlib

    hass = _native_hass(monkeypatch, "tesla_fleet")
    hass.data["power_sync"]["entry-1"]["_tesla_ev_recovery_sidecar_loaded"] = True
    entry = _Entry()
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    stock = importlib.import_module("power_sync.tesla_ev_stock_recovery")
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: None)
    monkeypatch.setattr(recovery, "pending_ble_attempt", lambda *_args: None)
    monkeypatch.setattr(recovery, "recovery_sidecar_snapshot", lambda *_args: {
        "incidents": {
            "incident-1": {
                "phase": "held",
                "current_candidate_vehicles": [VIN],
            },
        },
    })
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *_args, **_kwargs: lambda: True)
    recover = AsyncMock(return_value=False)
    monkeypatch.setattr(stock, "async_recover_vehicle_transport", recover)

    params = {"vehicle_vin": VIN}
    route = await actions._tesla_transport_route(hass, entry, params, "start")

    assert route == {
        "kind": "blocked", "vehicle_id": VIN, "reason": "stock_recovery_pending",
    }
    assert params["_tesla_transport_recovery_pending"] is True
    assert params["_tesla_stock_recovery_attempted"] is True
    recover.assert_awaited_once_with(hass, entry, VIN, incident_id="incident-1")
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_unenrolled_fixture_never_invokes_stock_recovery(monkeypatch):
    """Legacy fixtures without the production sidecar retain their old fence."""
    import importlib

    hass = _native_hass(monkeypatch, "tesla_fleet")
    entry = _Entry()
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    stock = importlib.import_module("power_sync.tesla_ev_stock_recovery")
    blocked = {"kind": "blocked", "vehicle_id": VIN, "reason": "ble_recovery"}
    monkeypatch.setattr(recovery, "get_vehicle_route", lambda *_args: blocked)
    monkeypatch.setattr(recovery, "pending_ble_attempt", lambda *_args: SimpleNamespace(
        attempt_id="attempt-1"
    ))
    monkeypatch.setattr(recovery, "async_isolate_ble", AsyncMock(return_value=False))
    monkeypatch.setattr(recovery, "recovery_sidecar_snapshot", lambda *_args: {"incidents": {}})
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *_args, **_kwargs: lambda: True)
    recover = AsyncMock(return_value=True)
    monkeypatch.setattr(stock, "async_recover_vehicle_transport", recover)

    params = {"vehicle_vin": VIN}
    assert await actions._tesla_transport_route(hass, entry, params, "rate") == blocked
    assert params["_tesla_transport_recovery_pending"] is True
    recover.assert_not_awaited()
    assert hass.services.calls == []


async def _async_true(*_args, **_kwargs):
    return True


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_ble_rate_skips_write_for_fresh_native_requested_current(monkeypatch):
    hass, ble_entity = _ble_native_hass(monkeypatch)
    import importlib

    observation = importlib.import_module("power_sync.tesla_ev_observation")
    monkeypatch.setattr(
        observation,
        "get_tesla_vehicle_observation",
        lambda *_args, **_kwargs: _native_observation(),
    )
    monkeypatch.setattr(actions, "prepare_ev_submission", _async_true)
    prepare_calls = []

    async def should_not_prepare(*_args, **_kwargs):
        prepare_calls.append(True)
        raise AssertionError("BLE preparation must not run for a native no-op")

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", should_not_prepare)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )
    finished = _journal_mocks(monkeypatch)
    params = {
        "vehicle_vin": VIN,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": _native_identity(hass),
    }

    result, fallback_ready = await actions._run_ble_transport_command(
        hass,
        _Entry(),
        params,
        "ble",
        "rate",
        lambda: actions._set_ev_charging_amps_ble(
            hass,
            "ble",
            30,
            params=params,
            authorize=lambda: True,
        ),
    )

    assert (result, fallback_ready) == (True, False)
    assert finished == ["not_sent"]
    assert prepare_calls == []
    assert hass.services.calls == []
    assert params["_tesla_ble_native_satisfied"]["target_current"] == 16
    assert params["_tesla_ble_native_satisfied"]["requested_current"] == 16


@pytest.mark.asyncio
async def test_ble_rate_uses_observation_fallback_after_ble_start_without_native_anchor(monkeypatch):
    hass, _ = _ble_native_hass(monkeypatch)
    import importlib

    observation = importlib.import_module("power_sync.tesla_ev_observation")
    monkeypatch.setattr(
        observation,
        "get_tesla_vehicle_observation",
        lambda *_args, **_kwargs: _native_observation(),
    )
    monkeypatch.setattr(actions, "prepare_ev_submission", _async_true)

    async def should_not_prepare(*_args, **_kwargs):
        raise AssertionError("BLE preparation must not run for a native no-op")

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", should_not_prepare)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )
    monkeypatch.setattr(
        actions,
        "_get_tesla_ev_entity",
        lambda *_args, **_kwargs: _async_return(ENTITY),
    )
    finished = _journal_mocks(monkeypatch)
    entry = SimpleNamespace(
        entry_id="entry-1", data={}, options={"ev_provider": actions.EV_PROVIDER_BOTH}
    )
    params = {"vehicle_vin": VIN}

    result, fallback_ready = await actions._run_ble_transport_command(
        hass,
        entry,
        params,
        "ble",
        "rate",
        lambda: actions._set_ev_charging_amps_ble(
            hass,
            "ble",
            16,
            params=params,
            authorize=lambda: True,
        ),
    )

    assert (result, fallback_ready) == (True, False)
    assert finished == ["not_sent"]
    assert hass.services.calls == []
    assert "_tesla_command_entity_id" not in params
    assert params["_tesla_ble_native_satisfied"]["target_current"] == 16


@pytest.mark.asyncio
async def test_ble_native_noop_validates_existing_rate_budget_without_new_ticket(monkeypatch):
    hass, ble_entity = _ble_native_hass(monkeypatch)
    import importlib

    observation = importlib.import_module("power_sync.tesla_ev_observation")
    admission = importlib.import_module("power_sync.native_rate_admission")
    budget_module = importlib.import_module("power_sync.native_rate_budget")
    registry_module = importlib.import_module("homeassistant.helpers.entity_registry")
    monkeypatch.setattr(
        observation,
        "get_tesla_vehicle_observation",
        lambda *_args, **_kwargs: _native_observation(),
    )
    monkeypatch.setattr(
        registry_module, "async_get", lambda _hass: hass.entity_registry
    )
    # Full collection can leave ``homeassistant.helpers.entity_registry`` on
    # the parent package pointing at an older module than the one imported
    # above.  Bind both import views and keep the fake registry's entity
    # lookup interface intact for the duration of this test.
    import sys
    helpers_module = importlib.import_module("homeassistant.helpers")
    monkeypatch.setitem(
        sys.modules, "homeassistant.helpers.entity_registry", registry_module
    )
    monkeypatch.setattr(
        helpers_module, "entity_registry", registry_module, raising=False
    )
    registry_entities = hass.entity_registry.entities
    monkeypatch.setattr(
        hass.entity_registry,
        "async_get",
        registry_entities.get,
        raising=False,
    )
    runtime = hass.data["power_sync"]["entry-1"]
    entry = SimpleNamespace(
        entry_id="entry-1",
        data={"tesla_energy_site_id": "site"},
        options={},
    )
    runtime["entry"] = entry
    budget = budget_module.SharedRateBudget(SimpleNamespace(async_load=lambda: None))
    budget.loaded = True
    budget.armed_sites.add("site")
    ble_start_entity = "switch.ble_charger"
    ble_device = SimpleNamespace(
        id="ble-device",
        identifiers={("esphome", "ble-device")},
        config_entries={"ble-entry"},
    )
    hass.device_registry.devices[ble_device.id] = ble_device
    hass.entity_registry.entities[ble_start_entity] = SimpleNamespace(
        entity_id=ble_start_entity,
        platform="esphome",
        config_entry_id="ble-entry",
        device_id=ble_device.id,
        unique_id="ble-start",
        disabled_by=None,
    )
    hass.states._states[ble_start_entity] = _State(ble_start_entity, "on")
    registration = hass.entity_registry.async_get(ble_start_entity)
    identity = ":".join(
        getattr(registration, field)
        for field in ("platform", "config_entry_id", "device_id", "unique_id")
    )
    budget.managed[VIN] = {
        "entry_id": "entry-1",
        "site": "site",
        "identity": identity,
        # This is the reservation created by the preceding BLE Start.  The
        # fresh native observation below intentionally uses a different
        # producer entity and must not be confused with this binding.
        "entity_id": ble_start_entity,
        "target_amps": 16,
        "reserved_w": 3680,
        "uncertain": True,
    }
    hass.data[admission.RUNTIME_KEY] = budget
    context_token = admission._EV_SUBMISSION_CONTEXT.set({
        "hass": hass,
        "entry": entry,
        "runtime": runtime,
        "vin": VIN,
        "params": {},
        "owner_snapshot": [],
    })
    before = deepcopy(budget.managed)
    try:
        params = {
            "vehicle_vin": VIN,
            "_tesla_ble_native_observation_target": {
                "entity_id": ENTITY,
                "identity": _native_identity(hass),
            },
        }
        assert await actions._tesla_ble_native_current_satisfied(
            hass,
            params,
            ble_entity,
            16,
            actions._tesla_legacy_entity_identity(hass, (ble_entity,)),
            lambda: True,
        )
        async def mutate_during_load():
            budget.managed[VIN]["target_amps"] = 17

        monkeypatch.setattr(budget, "load", mutate_during_load)
        assert not await actions._tesla_ble_native_current_satisfied(
            hass,
            params,
            ble_entity,
            16,
            actions._tesla_legacy_entity_identity(hass, (ble_entity,)),
            lambda: True,
        )
        budget.managed[VIN]["target_amps"] = 16
        async def no_mutation_load():
            return None

        monkeypatch.setattr(budget, "load", no_mutation_load)
        admission._EV_SUBMISSION_CONTEXT.get()["owner_snapshot"] = (
            ("entry-1", 1, "powersync", "automatic", "revoked"),
        )
        assert not await actions._tesla_ble_native_current_satisfied(
            hass,
            params,
            ble_entity,
            16,
            actions._tesla_legacy_entity_identity(hass, (ble_entity,)),
            lambda: True,
        )
        admission._EV_SUBMISSION_CONTEXT.get()["owner_snapshot"] = []
        entry.options["ev_provider"] = actions.EV_PROVIDER_BOTH
        monkeypatch.setattr(
            actions, "_get_tesla_ev_entity", lambda *_args, **_kwargs: _async_return(ENTITY)
        )
        finished = _journal_mocks(monkeypatch)
        wrapper_params = {"vehicle_vin": VIN}
        result, fallback_ready = await actions._run_ble_transport_command(
            hass,
            entry,
            wrapper_params,
            "ble",
            "rate",
            lambda: actions._set_ev_charging_amps_ble(
                hass,
                "ble",
                16,
                params=wrapper_params,
                authorize=lambda: True,
            ),
        )
        assert (result, fallback_ready) == (True, False)
        assert finished == ["not_sent"]
        assert hass.services.calls == []
    finally:
        admission._EV_SUBMISSION_CONTEXT.reset(context_token)
    assert budget.managed[VIN]["identity"] == before[VIN]["identity"]
    assert budget.managed[VIN]["entity_id"] == before[VIN]["entity_id"]
    assert budget.managed[VIN]["target_amps"] == 16
    assert "submitted_at" not in budget.managed[VIN]
    assert params["_tesla_ble_native_satisfied"]["target_current"] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "observation_factory"),
    [
        ("stale", lambda: _native_observation(timestamp=time.time() - 91)),
        ("future", lambda: _native_observation(timestamp=time.time() + 6)),
        ("wrong_vin", lambda: _native_observation(vin="5YJ3E1EA7NF0000B2")),
        ("missing_requested", lambda: _native_observation(requested_current=None)),
        (
            "actual_only",
            lambda: SimpleNamespace(
                **_native_observation().__dict__, requested_current=None,
                charger_actual_current=16,
            ),
        ),
    ],
)
async def test_ble_rate_never_uses_invalid_native_satisfaction_report(
    monkeypatch, case, observation_factory
):
    hass, ble_entity = _ble_native_hass(monkeypatch)
    import importlib

    observation = importlib.import_module("power_sync.tesla_ev_observation")
    monkeypatch.setattr(
        observation,
        "get_tesla_vehicle_observation",
        lambda *_args, **_kwargs: observation_factory(),
    )
    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _async_false)
    monkeypatch.setattr(actions, "prepare_ev_submission", _async_true)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )
    params = {
        "vehicle_vin": VIN,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": _native_identity(hass),
    }
    result = await actions._set_ev_charging_amps_ble(
        hass, "ble", 16, params=params, authorize=lambda: True
    )
    assert result is False, case
    assert hass.services.calls == []
    assert "_tesla_ble_native_satisfied" not in params


async def _async_false(*_args, **_kwargs):
    return False


@pytest.mark.asyncio
async def test_ble_rate_native_satisfaction_requires_authority_and_binding(monkeypatch):
    hass, _ = _ble_native_hass(monkeypatch)
    import importlib

    observation = importlib.import_module("power_sync.tesla_ev_observation")
    monkeypatch.setattr(
        observation,
        "get_tesla_vehicle_observation",
        lambda *_args, **_kwargs: _native_observation(),
    )
    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _async_false)
    monkeypatch.setattr(actions, "prepare_ev_submission", _async_true)
    params = {
        "vehicle_vin": VIN,
        "_tesla_command_entity_id": ENTITY,
        "_tesla_native_command_identity": _native_identity(hass),
    }
    original_authorized = actions._tesla_legacy_transport_authorized
    monkeypatch.setattr(actions, "_tesla_legacy_transport_authorized", _async_false)
    assert await actions._set_ev_charging_amps_ble(
        hass, "ble", 16, params=params, authorize=lambda: True
    ) is False
    monkeypatch.setattr(actions, "_tesla_legacy_transport_authorized", original_authorized)

    params["_tesla_native_command_identity"] = {
        **params["_tesla_native_command_identity"],
        "device_id": "rebound-native-device",
    }
    assert await actions._set_ev_charging_amps_ble(
        hass, "ble", 16, params=params, authorize=lambda: True
    ) is False
