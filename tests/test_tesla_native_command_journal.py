"""Focused durability checks for native Fleet/Teslemetry Tesla writes."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest

from test_ev_ocpp_actions import _Entry, _Hass, _State, actions


VIN = "5YJ3E1EA7NF0000A1"
ENTITY = "switch.car_charge"
AMPS_ENTITY = "number.car_charge_current"


def _native_hass(monkeypatch, platform: str, entity_id=ENTITY):
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
    monkeypatch.setattr(actions.er, "async_get", lambda _hass: hass.entity_registry)
    monkeypatch.setattr(actions.dr, "async_get", lambda _hass: hass.device_registry)
    return hass


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
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry"])
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
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry"])
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
@pytest.mark.parametrize("platform", ["tesla_fleet", "teslemetry"])
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
