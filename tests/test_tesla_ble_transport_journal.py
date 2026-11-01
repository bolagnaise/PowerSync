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


async def _prepare_for_test(*_args, **kwargs):
    holder = kwargs.get("charge_binding_holder")
    if holder is not None:
        holder[:] = [object()]
    return True


@pytest.mark.asyncio
async def test_ble_rate_journal_pins_target_and_bounded_effect(monkeypatch):
    hass = _ble_hass()
    _patch_ble_scope(monkeypatch)
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(attempt_id="rate-target")
    begin_call = {}

    async def begin(*args, **kwargs):
        begin_call["route"] = args[3]
        begin_call.update(kwargs)
        return ticket

    finished = []

    async def finish(_ticket, outcome):
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)
    bound = {"kind": "rate", "min_amps": 8, "max_amps": 16}

    result = await actions._run_ble_transport_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN},
        "bridge",
        "rate",
        _true_async,
        effect_bound=bound,
    )

    assert result == (True, False)
    assert finished == ["confirmed"]
    assert begin_call["route"]["effect_bound"] == bound
    target = begin_call["target"]
    assert target.vin == VIN
    assert target.operation == "rate"
    assert target.entity_id == "number.bridge_charging_amps"
    assert target.entity_domain == "number"


def test_ble_rate_effect_bound_covers_structured_entity_range_retry():
    hass = _ble_hass()

    assert actions._tesla_ble_rate_effect_bound(
        hass,
        "number.bridge_charging_amps",
        40,
        allow_stale_entity_max_override=True,
        configured_max_amps=40,
    ) == {"kind": "rate", "min_amps": 32, "max_amps": 40}


@pytest.mark.asyncio
async def test_ble_bound_rejects_entity_range_change_during_journal_persist(monkeypatch):
    hass = _ble_hass()
    _patch_ble_scope(monkeypatch)
    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _prepare_for_test)
    monkeypatch.setattr(actions, "_tesla_ble_scope_current", _true_async)
    monkeypatch.setattr(actions, "_tesla_ble_charge_data_binding", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(actions, "prepare_ev_submission", _true_async)
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    ticket = SimpleNamespace(attempt_id="range-change")
    finished = []
    hass.states.get("number.bridge_charging_amps").attributes["max"] = 16
    bound = actions._tesla_ble_rate_effect_bound(
        hass, "number.bridge_charging_amps", 16
    )

    async def begin(*_args, **_kwargs):
        # Model the HA registry/range changing while the durable begin is
        # awaited.  The actual helper must remain inside the persisted bound.
        hass.states.get("number.bridge_charging_amps").attributes["max"] = 32
        return ticket

    async def finish(_ticket, outcome):
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_begin_attempt", begin)
    monkeypatch.setattr(recovery, "async_finish_attempt", finish)

    with_services = hass.services.calls
    result = await actions._run_ble_transport_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN},
        "bridge",
        "rate",
        lambda: actions._set_ev_charging_amps_ble(hass, "bridge", 32),
        effect_bound=bound,
    )

    assert result == (False, False)
    assert finished == ["not_sent"]
    assert with_services == []


@pytest.mark.asyncio
async def test_missing_ble_bound_cannot_write_after_entity_appears(monkeypatch):
    hass = _ble_hass()
    _patch_ble_scope(monkeypatch)
    send_called = False

    async def send():
        nonlocal send_called
        send_called = True
        return True

    result = await actions._run_ble_transport_command(
        hass,
        _Entry(),
        {"vehicle_vin": VIN},
        "bridge",
        "rate",
        send,
        effect_bound=None,
    )

    assert result == (False, False)
    assert send_called is False
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_ble_unknown_persists_target_and_effect_bound(monkeypatch):
    hass = _ble_hass()
    runtime = {"setup_generation": 1, "runtime_generation": 1}
    hass.data["power_sync"]["entry-1"] = runtime
    entry = _Entry()
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    monkeypatch.setattr(recovery, "_persist", lambda *_args, **_kwargs: _true_async())
    _patch_ble_scope(monkeypatch)
    bound = {"kind": "rate", "min_amps": 8, "max_amps": 16}

    async def send():
        return None

    result = await actions._run_ble_transport_command(
        hass, entry, {"vehicle_vin": VIN}, "bridge", "rate", send,
        effect_bound=bound,
    )

    assert result == (None, False)
    ledger = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN]
    attempt = next(iter(ledger["attempts"].values()))
    assert attempt["outcome"] == "sent_unknown"
    assert attempt["target"]["vin"] == VIN
    assert attempt["target"]["operation"] == "rate"
    assert attempt["route"]["effect_bound"] == bound


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
async def test_ble_unstructured_range_error_does_not_retry_and_keeps_prior_submitted_hazard(
    monkeypatch,
):
    hass = _ble_hass()
    runtime = hass.data["power_sync"]["entry-1"]
    runtime.update({"setup_generation": 1, "runtime_generation": 1})
    entry = _Entry()
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    entry.options = {"ev_provider": actions.EV_PROVIDER_BOTH}
    isolated = []

    async def isolate(ticket, **_kwargs):
        isolated.append(ticket.attempt_id)
        return True

    monkeypatch.setattr(recovery, "async_isolate_ble", isolate)
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
        return True

    monkeypatch.setattr(actions, "prepare_ev_submission", submit)

    async def range_error(domain, service, data, blocking=True):
        hass.services.calls.append((domain, service, data))
        raise RuntimeError("outside valid range")

    hass.services.async_call = range_error
    result = await actions._run_ble_transport_command(
        hass,
        entry,
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

    assert result == (None, True)
    assert submission_calls == 1
    assert len(hass.services.calls) == 1
    assert len(isolated) == 1
    ledger = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN]
    attempt = next(iter(ledger["attempts"].values()))
    assert attempt["outcome"] == "sent_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("second_rejection", [False, True])
async def test_ble_structured_range_rejection_handles_bounded_retry(
    monkeypatch, second_rejection,
):
    hass = _ble_hass()
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)
    import homeassistant.exceptions as exceptions

    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _prepare_for_test)
    monkeypatch.setattr(
        actions,
        "_tesla_ble_scope_current",
        lambda *_args, **_kwargs: _true_async(),
    )
    monkeypatch.setattr(
        actions,
        "_tesla_ble_charge_data_binding",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        actions,
        "_wait_for_ble_command_readback",
        lambda *_args, **_kwargs: _true_async(),
    )
    monkeypatch.setattr(
        actions,
        "prepare_ev_submission",
        lambda *_args, **_kwargs: _true_async(),
    )

    class StructuredRangeError(Exception):
        translation_domain = "number"
        translation_key = "out_of_range"

        def __init__(self, value):
            super().__init__("structured range rejection")
            self.translation_placeholders = {
                "entity_id": "number.bridge_charging_amps",
                "value": str(value),
                "min_value": "1",
                "max_value": "16",
            }

    monkeypatch.setattr(
        exceptions, "ServiceValidationError", StructuredRangeError, raising=False
    )
    service_calls = []

    async def service(domain, service_name, data, blocking=True):
        service_calls.append((domain, service_name, data))
        if len(service_calls) == 1 or second_rejection:
            raise StructuredRangeError(data["value"])

    hass.services.async_call = service
    bound = actions._tesla_ble_rate_effect_bound(
        hass,
        "number.bridge_charging_amps",
        40,
        allow_stale_entity_max_override=True,
        configured_max_amps=40,
    )

    result = await actions._run_ble_transport_command(
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
        effect_bound=bound,
    )

    assert result == ((False, False) if second_rejection else (True, False))
    assert finished == (["not_sent"] if second_rejection else ["confirmed"])
    assert len(service_calls) == 2


@pytest.mark.asyncio
async def test_ble_admission_exception_after_service_handoff_keeps_real_journal_hazard(
    monkeypatch,
):
    hass = _ble_hass()
    runtime = hass.data["power_sync"]["entry-1"]
    runtime.update({"setup_generation": 1, "runtime_generation": 1})
    entry = _Entry()
    import importlib

    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    _patch_ble_scope(monkeypatch)
    monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _prepare_for_test)
    monkeypatch.setattr(
        actions,
        "_tesla_ble_scope_current",
        lambda *_args, **_kwargs: _true_async(),
    )
    monkeypatch.setattr(
        actions,
        "_tesla_ble_charge_data_binding",
        lambda *_args, **_kwargs: object(),
    )
    monkeypatch.setattr(
        actions,
        "prepare_ev_submission",
        lambda *_args, **_kwargs: _true_async(),
    )

    service_calls = []

    async def service(*args, **kwargs):
        service_calls.append((args, kwargs))
        error = RuntimeError("site import admission denied after handoff")
        error.site_import_admission_denied = True
        raise error

    hass.services.async_call = service
    with pytest.raises(RuntimeError, match="after handoff"):
        await actions._run_ble_transport_command(
            hass,
            entry,
            {"vehicle_vin": VIN},
            "bridge",
            "rate",
            lambda: actions._set_ev_charging_amps_ble(hass, "bridge", 16),
        )

    ledger = runtime[recovery.RECOVERY_KEY]["vehicles"][VIN]
    attempt = next(iter(ledger["attempts"].values()))
    assert attempt["outcome"] == "sent_unknown"
    assert len(service_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["unstructured", "structured"])
async def test_teslemetry_bt_range_outcome_preserves_handoff(
    monkeypatch, error_kind,
):
    prefix = "lrw3f7fs1nc484342"
    number_entity = f"number.{prefix}_charge_current_request"
    import test_ev_ocpp_actions as ev_actions
    import homeassistant.exceptions as exceptions

    registry = ev_actions._legacy_teslemetry_bt_registry((prefix,))
    registry[number_entity] = SimpleNamespace(
        entity_id=number_entity,
        platform="tesla_bluetooth",
        config_entry_id="tesla-bt-entry",
        device_id="tesla-bt-device",
        unique_id=number_entity,
        disabled_by=None,
    )
    hass = _Hass(
        [
            _State(number_entity, "16", {"min": 1, "max": 16}),
            _State(f"sensor.{prefix}_charging_state", "Stopped"),
            _State(f"switch.{prefix}_charge", "off"),
        ],
        registry,
    )
    finished = _journal_mocks(monkeypatch)
    _patch_ble_scope(monkeypatch)
    prepare_calls = 0

    async def prepare(*_args, **_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        return True

    monkeypatch.setattr(actions, "prepare_ev_submission", prepare)

    class StructuredRangeError(Exception):
        translation_domain = "number"
        translation_key = "out_of_range"

        def __init__(self, value):
            super().__init__("structured range rejection")
            self.translation_placeholders = {
                "entity_id": number_entity,
                "value": str(value),
                "min_value": "1",
                "max_value": "16",
            }

    monkeypatch.setattr(
        exceptions, "ServiceValidationError", StructuredRangeError, raising=False
    )

    async def range_error(domain, service, data, blocking=True):
        hass.services.calls.append((domain, service, data))
        if error_kind == "structured":
            raise StructuredRangeError(data["value"])
        raise RuntimeError("outside valid range")

    hass.services.async_call = range_error
    result = await actions._run_teslemetry_bt_transport_command(
        hass,
        _Entry(),
        {"vehicle_vin": prefix.upper()},
        prefix,
        "rate",
        lambda: actions._set_ev_charging_amps_teslemetry_bt(
            hass,
            prefix,
            40,
            allow_stale_entity_max_override=True,
            configured_max_amps=40,
        ),
    )

    structured = error_kind == "structured"
    assert result == ((False, False) if structured else (None, False))
    assert finished == (["not_sent"] if structured else ["sent_unknown"])
    assert prepare_calls == (2 if structured else 1)
    assert len(hass.services.calls) == (2 if structured else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["ble", "teslemetry_bt"])
async def test_structured_range_retry_cancellation_after_provider_handoff_is_unknown(
    monkeypatch, transport,
):
    """A second fallback call that reaches HA cannot be retired as not_sent."""
    import homeassistant.exceptions as exceptions
    import importlib
    import test_ev_ocpp_actions as ev_actions

    if transport == "ble":
        hass = _ble_hass()
        prefix = "bridge"
        number_entity = "number.bridge_charging_amps"
        vehicle = VIN
    else:
        prefix = "lrw3f7fs1nc484342"
        number_entity = f"number.{prefix}_charge_current_request"
        registry = ev_actions._legacy_teslemetry_bt_registry((prefix,))
        registry[number_entity] = SimpleNamespace(
            entity_id=number_entity,
            platform="tesla_bluetooth",
            config_entry_id="tesla-bt-entry",
            device_id="tesla-bt-device",
            unique_id=number_entity,
            disabled_by=None,
        )
        hass = _Hass(
            [
                _State(number_entity, "16", {"min": 1, "max": 16}),
                _State(f"sensor.{prefix}_charging_state", "Stopped"),
                _State(f"switch.{prefix}_charge", "off"),
            ],
            registry,
        )
        vehicle = prefix.upper()

    runtime = hass.data["power_sync"]["entry-1"]
    runtime.update({"setup_generation": 1, "runtime_generation": 1})
    entry = _Entry()
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    _patch_ble_scope(monkeypatch)

    if transport == "ble":
        monkeypatch.setattr(actions, "_tesla_ble_prepare_mutation", _prepare_for_test)
        monkeypatch.setattr(
            actions,
            "_tesla_ble_scope_current",
            lambda *_args, **_kwargs: _true_async(),
        )
        monkeypatch.setattr(
            actions,
            "_tesla_ble_charge_data_binding",
            lambda *_args, **_kwargs: object(),
        )

    monkeypatch.setattr(
        actions,
        "prepare_ev_submission",
        lambda *_args, **_kwargs: _true_async(),
    )

    class StructuredRangeError(Exception):
        translation_domain = "number"
        translation_key = "out_of_range"

        def __init__(self, value):
            super().__init__("structured range rejection")
            self.translation_placeholders = {
                "entity_id": number_entity,
                "value": str(value),
                "min_value": "1",
                "max_value": "16",
            }

    monkeypatch.setattr(
        exceptions, "ServiceValidationError", StructuredRangeError, raising=False
    )
    service_calls = []

    async def service(domain, service_name, data, blocking=True):
        service_calls.append((domain, service_name, data))
        if len(service_calls) == 1:
            raise StructuredRangeError(data["value"])
        raise asyncio.CancelledError

    hass.services.async_call = service
    if transport == "ble":
        bound = actions._tesla_ble_rate_effect_bound(
            hass,
            number_entity,
            40,
            allow_stale_entity_max_override=True,
            configured_max_amps=40,
        )
        send = lambda: actions._set_ev_charging_amps_ble(
            hass,
            prefix,
            40,
            allow_stale_entity_max_override=True,
            configured_max_amps=40,
        )
    else:
        bound = None
        send = lambda: actions._set_ev_charging_amps_teslemetry_bt(
            hass,
            prefix,
            40,
            allow_stale_entity_max_override=True,
            configured_max_amps=40,
        )

    with pytest.raises(asyncio.CancelledError):
        if transport == "ble":
            await actions._run_ble_transport_command(
                hass,
                entry,
                {"vehicle_vin": vehicle},
                prefix,
                "rate",
                send,
                effect_bound=bound,
            )
        else:
            await actions._run_teslemetry_bt_transport_command(
                hass,
                entry,
                {"vehicle_vin": vehicle},
                prefix,
                "rate",
                send,
            )

    assert [call[2]["value"] for call in service_calls] == [40, 32 if transport == "ble" else 16]
    ledger = runtime[recovery.RECOVERY_KEY]["vehicles"][vehicle]
    attempt = next(iter(ledger["attempts"].values()))
    assert attempt["outcome"] == "sent_unknown"


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
