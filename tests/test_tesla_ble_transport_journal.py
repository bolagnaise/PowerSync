"""Cancellation phase fencing for journaled Tesla BLE mutations."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from test_ev_ocpp_actions import _Entry, _Hass, _State, actions


VIN = "5YJ3E1EA7NF0000A1"


def _ble_hass():
    entities = {}
    states = []
    for entity_id in (
        "switch.bridge_charger",
        "number.bridge_charging_limit",
        "number.bridge_charging_amps",
    ):
        entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            platform="esphome",
            config_entry_id="ble-entry",
            device_id="ble-device",
            unique_id=entity_id,
            disabled_by=None,
        )
        attributes = {"min": 1, "max": 32} if entity_id.endswith("amps") else {}
        states.append(_State(entity_id, "off" if entity_id.startswith("switch") else "16", attributes))
    hass = _Hass(states, entities)
    return hass


def _journal_mocks(monkeypatch):
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(attempt_id="ble-attempt")
    finished = []

    async def begin(*_args, **_kwargs):
        return ticket

    async def finish(_ticket, outcome):
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)
    return finished


def _patch_ble_scope(monkeypatch):
    monkeypatch.setattr(actions, "bind_vehicle", lambda *_args: None)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *_args, **_kwargs: lambda: True,
    )


@pytest.mark.asyncio
async def test_uninstrumented_send_cancellation_stays_unknown(monkeypatch):
    hass = _ble_hass()
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)

    async def send():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await actions._run_ble_transport_command(
            hass, _Entry(), {"vehicle_vin": VIN}, "bridge", "start", send
        )

    assert finished == ["sent_unknown"]
    assert actions._tesla_ble_mutation_phase.get() is None


@pytest.mark.asyncio
async def test_cancellation_during_known_ble_prewrite_is_not_sent(monkeypatch):
    hass = _ble_hass()
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)

    async def prepare(*_args, **kwargs):
        if (holder := kwargs.get("charge_binding_holder")) is not None:
            holder[:] = [object()]
        raise asyncio.CancelledError

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", prepare)

    async def send():
        return await actions._start_ev_charging_ble(hass, "bridge")

    with pytest.raises(asyncio.CancelledError):
        await actions._run_ble_transport_command(
            hass, _Entry(), {"vehicle_vin": VIN}, "bridge", "start", send
        )

    assert finished == ["not_sent"]
    assert hass.services.calls == []
    assert actions._tesla_ble_mutation_phase.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "send"),
    (
        ("start", lambda hass: actions._start_ev_charging_ble(hass, "bridge")),
        ("stop", lambda hass: actions._stop_ev_charging_ble(hass, "bridge")),
        ("limit", lambda hass: actions._set_ev_charge_limit_ble(hass, "bridge", 90)),
        ("rate", lambda hass: actions._set_ev_charging_amps_ble(hass, "bridge", 16)),
    ),
)
async def test_each_esp_home_helper_marks_mutation_before_service_cancellation(
    monkeypatch, operation, send
):
    hass = _ble_hass()
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)

    async def prepare(*_args, **kwargs):
        if (holder := kwargs.get("charge_binding_holder")) is not None:
            holder[:] = [object()]
        return True

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", prepare)
    monkeypatch.setattr(actions, "_tesla_ble_scope_current", lambda *_args, **_kwargs: _true_async())
    monkeypatch.setattr(actions, "_tesla_ble_charge_data_binding", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(actions, "prepare_ev_submission", lambda *_args, **_kwargs: _true_async())
    monkeypatch.setattr(actions, "_wait_for_ble_command_readback", _cancelled_readback)

    with pytest.raises(asyncio.CancelledError):
        await actions._run_ble_transport_command(
            hass,
            _Entry(),
            {"vehicle_vin": VIN},
            "bridge",
            operation,
            lambda: send(hass),
        )

    assert finished == ["sent_unknown"]
    assert len(hass.services.calls) == 1
    assert actions._tesla_ble_mutation_phase.get() is None


@pytest.mark.asyncio
async def test_ble_range_fallback_cancellation_keeps_prior_submitted_hazard(monkeypatch):
    hass = _ble_hass()
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)

    async def prepare(*_args, **kwargs):
        if (holder := kwargs.get("charge_binding_holder")) is not None:
            holder[:] = [object()]
        return True

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", prepare)
    monkeypatch.setattr(actions, "_tesla_ble_scope_current", lambda *_args, **_kwargs: _true_async())
    monkeypatch.setattr(actions, "_tesla_ble_charge_data_binding", lambda *_args, **_kwargs: object())
    submission_calls = 0

    async def submit(*_args, **_kwargs):
        nonlocal submission_calls
        submission_calls += 1
        if submission_calls == 2:
            raise asyncio.CancelledError
        return True

    monkeypatch.setattr(actions, "prepare_ev_submission", submit)

    async def range_error(domain, service, data, blocking=True):
        hass.services.calls.append((domain, service, data))
        raise RuntimeError("outside valid range")

    hass.services.async_call = range_error
    with pytest.raises(asyncio.CancelledError):
        await actions._run_ble_transport_command(
            hass,
            _Entry(),
            {"vehicle_vin": VIN},
            "bridge",
            "rate",
            lambda: actions._set_ev_charging_amps_ble(
                hass,
                "bridge",
                40,
                allow_stale_entity_max_override=True,
                configured_max_amps=40,
            ),
        )

    assert finished == ["sent_unknown"]
    assert submission_calls == 2
    assert len(hass.services.calls) == 1


async def _true_async():
    return True


async def _cancelled_readback(*_args, **_kwargs):
    raise asyncio.CancelledError


def test_ble_mutation_phase_restores_nested_contexts():
    assert actions._tesla_ble_mutation_phase.get() is None
    outer = {"phase": "unknown"}
    outer_token = actions._tesla_ble_mutation_phase.set(outer)
    try:
        actions._tesla_ble_mark_mutation_prewrite()
        assert outer["phase"] == "prewrite"
        inner = {"phase": "unknown"}
        inner_token = actions._tesla_ble_mutation_phase.set(inner)
        try:
            actions._tesla_ble_mark_mutation_submitted()
            assert inner["phase"] == "submitted"
            assert outer["phase"] == "prewrite"
        finally:
            actions._tesla_ble_mutation_phase.reset(inner_token)
        assert actions._tesla_ble_mutation_phase.get() is outer
    finally:
        actions._tesla_ble_mutation_phase.reset(outer_token)
    assert actions._tesla_ble_mutation_phase.get() is None


def test_ble_mutation_phase_never_downgrades_submitted_to_prewrite():
    phase = {"phase": "unknown"}
    token = actions._tesla_ble_mutation_phase.set(phase)
    try:
        actions._tesla_ble_mark_mutation_prewrite()
        actions._tesla_ble_mark_mutation_submitted()
        actions._tesla_ble_mark_mutation_prewrite()
        assert phase["phase"] == "submitted"
    finally:
        actions._tesla_ble_mutation_phase.reset(token)
