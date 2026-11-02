"""Modern Teslemetry Bluetooth action outcomes stay source- and hazard-safe."""
from __future__ import annotations

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace
import time
from unittest.mock import AsyncMock

import pytest

from test_ev_ocpp_actions import _Entry, _Hass, _State, actions
from power_sync.tesla_ev_dispatch import DispatchOutcome, DispatchReceipt
from power_sync.tesla_ev_observation import TeslaVehicleObservation
from power_sync.teslemetry_bluetooth_commands import BluetoothCommandTarget


VIN = "5YJ3E1EA7NF0000A1"


def _both_native_action_harness():
    """Build one VIN exposed by modern commands, Teslemetry, and Fleet."""
    native_device = SimpleNamespace(
        id="teslemetry-device",
        identifiers={("teslemetry", VIN)},
        config_entries={"native-entry", "commands-entry"},
    )
    fleet_device = SimpleNamespace(
        id="fleet-device",
        identifiers={("tesla_fleet", VIN)},
        config_entries={"fleet-entry"},
    )
    entities = {}
    states = []

    def add(entity_id, unique_id, platform, entry_id, device_id, value, attributes=None):
        entities[entity_id] = SimpleNamespace(
            entity_id=entity_id,
            unique_id=unique_id,
            platform=platform,
            config_entry_id=entry_id,
            device_id=device_id,
            disabled_by=None,
        )
        states.append(_State(entity_id, value, attributes))

    for operation, domain, key in (
        ("start", "button", "charge_start"),
        ("stop", "button", "charge_stop"),
        ("rate", "number", "set_charging_amps"),
        ("limit", "number", "set_charge_limit"),
    ):
        add(
            f"{domain}.modern_{operation}",
            f"{VIN}-bluetooth-{key}",
            "teslemetry_bluetooth",
            "commands-entry",
            "teslemetry-device",
            "unknown",
        )
    add(
        "switch.native_charge",
        f"{VIN}-charge_state_user_charge_enable_request",
        "teslemetry", "native-entry", "teslemetry-device", "off",
    )
    add(
        "number.native_current",
        f"{VIN}-charge_state_charge_current_request",
        "teslemetry", "native-entry", "teslemetry-device", "16",
        {"min": 0, "max": 80},
    )
    add(
        "number.native_limit",
        f"{VIN}-charge_state_charge_limit_soc",
        "teslemetry", "native-entry", "teslemetry-device", "80",
        {"min": 50, "max": 100},
    )
    # A second integration exposes the same VIN. It must never win native
    # route resolution while the selected Teslemetry device is present.
    add("switch.fleet_charge", "fleet-charge", "tesla_fleet", "fleet-entry", "fleet-device", "off")
    add(
        "number.fleet_current", "fleet-current", "tesla_fleet", "fleet-entry", "fleet-device", "16",
        {"min": 0, "max": 32},
    )
    add(
        "number.fleet_limit", "fleet-limit", "tesla_fleet", "fleet-entry", "fleet-device", "80",
        {"min": 50, "max": 100},
    )

    hass = _Hass(
        states,
        registry_entities=entities,
        registry_devices={
            "teslemetry-device": native_device,
            "fleet-device": fleet_device,
        },
    )
    provider_entries = {
        "teslemetry": [SimpleNamespace(entry_id="native-entry", domain="teslemetry")],
        "teslemetry_bluetooth": [SimpleNamespace(entry_id="commands-entry", domain="teslemetry_bluetooth")],
        "tesla_fleet": [SimpleNamespace(entry_id="fleet-entry", domain="tesla_fleet")],
    }
    hass.config_entries = SimpleNamespace(
        async_entries=lambda domain: provider_entries.get(domain, [])
    )
    entry = _Entry()
    entry.options = {"ev_provider": actions.EV_PROVIDER_BOTH}
    return hass, entry


def _pin_registry_bindings(monkeypatch, hass):
    """Keep lazy HA registry imports local to this synthetic HA instance."""
    ha_root = sys.modules.get("homeassistant")
    helpers = sys.modules.get("homeassistant.helpers")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    entity_registry.async_get = lambda _hass: hass.entity_registry
    device_registry.async_get = lambda _hass: hass.device_registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", entity_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    if helpers is not None:
        monkeypatch.setattr(helpers, "entity_registry", entity_registry, raising=False)
        monkeypatch.setattr(helpers, "device_registry", device_registry, raising=False)
    if ha_root is not None:
        monkeypatch.setattr(ha_root, "helpers", helpers, raising=False)
    # actions imported these bindings at module load; pin those direct refs as
    # well as the lazy modules used by teslemetry_bluetooth_commands.
    monkeypatch.setattr(actions, "er", entity_registry)
    monkeypatch.setattr(actions, "dr", device_registry)


def _pin_solar_ownership(monkeypatch, action_name):
    """Use the real solar ownership gate with a bounded in-memory store.

    Collection-wide HA stubs can leave ``homeassistant.helpers.storage.Store``
    with an incompatible constructor.  The EV action decorator owns the
    Teslemetry solar module globals, so pin that exact binding rather than
    changing the production guard or bypassing it.
    """
    guard_globals = getattr(actions, action_name).__globals__
    solar_ownership_type = guard_globals["SolarOwnership"]

    class _Store:
        def __init__(self):
            self.value = None

        async def async_load(self):
            return self.value

        async def async_save(self, value):
            self.value = value

    gate = solar_ownership_type(_Store())

    async def ownership(_hass, _entry_id):
        return gate

    monkeypatch.setitem(guard_globals, "ownership", ownership)


def _target(operation: str) -> BluetoothCommandTarget:
    domain = "button" if operation in ("start", "stop") else "number"
    return BluetoothCommandTarget(
        vin=VIN,
        operation=operation,
        entity_id=f"{domain}.car_{operation}",
        entity_domain=domain,
        platform="teslemetry_bluetooth",
        unique_id=f"{VIN}-bluetooth-{operation}",
        config_entry_id="commands-entry",
        device_id="teslemetry-device",
        native_config_entry_ids=("native-entry",),
    )


def _observation(state: str, *, current: float | None, power: float | None):
    return TeslaVehicleObservation(
        vin=VIN,
        domain="teslemetry",
        config_entry_id="native-entry",
        device_id="teslemetry-device",
        producer_identity="teslemetry:native-entry:teslemetry-device:" + VIN,
        source_charge_timestamp=time.time() + 1,
        charge_state=state,
        charger_actual_current=current,
        charger_power=power,
        charge_limit_soc=80,
        requested_current=16,
    )


@pytest.fixture
def modern(monkeypatch):
    hass = _Hass([])
    entry = _Entry()
    params = {"vehicle_vin": VIN}
    target = _target("start")
    ticket = SimpleNamespace(attempt_id="attempt-1")
    finished = []
    current = {"outcome": DispatchOutcome.NOT_SENT, "observation": None}

    commands = importlib.import_module("power_sync.teslemetry_bluetooth_commands")
    dispatch = importlib.import_module("power_sync.tesla_ev_dispatch")
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(commands, "resolve_command", lambda *_: target)
    monkeypatch.setattr(actions, "_tesla_transport_authority", lambda *args, **kwargs: lambda: True)
    monkeypatch.setattr(actions, "prepare_ev_submission", _allow_submission)
    monkeypatch.setattr(recovery, "async_begin_attempt", _begin(ticket))

    async def finish(_ticket, outcome):
        finished.append(outcome)
        return True

    monkeypatch.setattr(recovery, "async_finish_attempt", finish)

    async def dispatch_command(_hass, resolved, **kwargs):
        assert resolved == target
        before = kwargs["before_dispatch"]
        assert await before() is True
        if isinstance(current["outcome"], BaseException):
            raise current["outcome"]
        return DispatchReceipt(
            current["outcome"], target,
            submitted_at=time.time(), observation=current["observation"], reason="test",
        )

    monkeypatch.setattr(dispatch, "async_dispatch_bluetooth_command", dispatch_command)
    return hass, entry, params, target, current, finished


async def _allow_submission(*args, **kwargs):
    return True


def _begin(ticket):
    async def begin(*args, **kwargs):
        return ticket
    return begin


@pytest.mark.asyncio
async def test_modern_cleanup_stop_writes_once_after_attempt_persist_refusal(monkeypatch):
    hass, entry = _both_native_action_harness()
    entry.options = {"ev_provider": actions.EV_PROVIDER_TESLEMETRY_BT}
    _pin_registry_bindings(monkeypatch, hass)
    commands = importlib.import_module("power_sync.teslemetry_bluetooth_commands")
    dispatch = importlib.import_module("power_sync.tesla_ev_dispatch")
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    target = _target("stop")
    params = {
        "vehicle_vin": VIN,
        "_ev_cleanup_only": True,
        "_quick_control_obligation_id": "modern-obligation",
    }
    shared_tracker_calls = []
    monkeypatch.setattr(commands, "resolve_command", lambda *_: target)
    monkeypatch.setattr(
        actions,
        "_tesla_transport_authority",
        lambda *args, **kwargs: lambda: True,
    )

    async def refused(*_args, **_kwargs):
        raise recovery.RecoveryPersistenceRefused(
            "journal unavailable",
            vehicle_id=VIN,
            route={
                "kind": "teslemetry_bluetooth",
                "entity_id": target.entity_id,
                "vehicle_id": VIN,
                "cleanup_scope_valid": True,
                "cleanup_obligation_id": "modern-obligation",
            },
        )

    async def shared_tracker(
        _hass, _entry, refusal, submit, *, vehicle_id, generation
    ):
        shared_tracker_calls.append((refusal, vehicle_id, generation))
        return await submit()

    monkeypatch.setattr(recovery, "async_begin_attempt", refused)
    monkeypatch.setattr(recovery, "async_track_cleanup_submission", shared_tracker)

    async def dispatch_command(_hass, resolved, **kwargs):
        assert resolved == target
        assert await kwargs["before_dispatch"]() is True

        async def provider_call():
            return await hass.services.async_call(
                "button", "press", {"entity_id": target.entity_id}, blocking=True
            )

        await kwargs["submit"](provider_call)
        return DispatchReceipt(
            DispatchOutcome.ACCEPTED_UNCONFIRMED,
            target,
            submitted_at=time.time(),
            reason="cleanup fallback",
        )

    monkeypatch.setattr(dispatch, "async_dispatch_bluetooth_command", dispatch_command)
    result = await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "stop"
    )

    assert result == (True, None)
    assert hass.services.calls == [
        ("button", "press", {"entity_id": target.entity_id})
    ]
    assert len(shared_tracker_calls) == 1
    assert isinstance(shared_tracker_calls[0][0], recovery.RecoveryPersistenceRefused)
    assert params["_tesla_cleanup_fallback_consumed"] is True


@pytest.mark.asyncio
async def test_clean_not_sent_returns_false_without_recovery_pending(modern):
    hass, entry, params, _target_value, current, finished = modern
    current["outcome"] = DispatchOutcome.NOT_SENT

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start") == (True, False)
    assert "_tesla_transport_recovery_pending" not in params
    assert [getattr(value, "value", value) for value in finished] == ["not_sent"]


@pytest.mark.asyncio
async def test_modern_submission_rechecks_deadline_after_ticket_persistence(
    modern, monkeypatch
):
    """The command-only child cannot write after ticket-save authority changes."""
    hass, entry, params, target, current, finished = modern
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    dispatch = importlib.import_module("power_sync.tesla_ev_dispatch")
    params["_boost_start_deadline"] = time.time() + 60

    async def tracked(_hass, _entry, submit, *_args, **_kwargs):
        params["_boost_start_deadline"] = time.time() - 1
        return await submit()

    monkeypatch.setattr(recovery, "async_track_submission", tracked)

    async def dispatch_after_before(_hass, resolved, **kwargs):
        assert resolved == target
        assert await kwargs["before_dispatch"]() is True

        async def provider_call():
            return await hass.services.async_call(
                "button", "press", {"entity_id": target.entity_id}, blocking=True
            )

        try:
            await kwargs["submit"](provider_call)
        except recovery.RecoverySubmissionDenied:
            return DispatchReceipt(
                DispatchOutcome.NOT_SENT, target, reason="authority_changed"
            )
        raise AssertionError("provider write should have been blocked")

    monkeypatch.setattr(dispatch, "async_dispatch_bluetooth_command", dispatch_after_before)

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start"
    ) == (True, False)
    assert finished == ["not_sent"]
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_both_prefers_shared_native_teslemetry_router(modern, monkeypatch):
    hass, entry, params, _target_value, current, finished = modern
    entry.options = {"ev_provider": actions.EV_PROVIDER_BOTH}
    commands = importlib.import_module("power_sync.teslemetry_bluetooth_commands")
    monkeypatch.setattr(commands, "native_command_entity", lambda *_: "switch.native_charge")

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start"
    ) == (False, False)
    assert params["_tesla_native_router_selected"] is True
    assert params["_tesla_native_command_entity_id"] == "switch.native_charge"
    assert not current["observation"]
    assert not finished


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "action_name", "params", "expected_call"),
    (
        ("start", "_action_start_ev_charging", {},
         ("switch", "turn_on", {"entity_id": "switch.native_charge"})),
        ("stop", "_action_stop_ev_charging", {"_force_tesla_stop_request": True},
         ("switch", "turn_off", {"entity_id": "switch.native_charge"})),
        ("rate", "_action_set_ev_charging_amps", {"amps": 16},
         ("number", "set_value", {"entity_id": "number.native_current", "value": 16})),
        ("limit", "_action_set_ev_charge_limit", {"percent": 90},
         ("number", "set_value", {"entity_id": "number.native_limit", "value": 90})),
    ),
)
async def test_both_provider_uses_shared_native_router_for_all_operations(
    operation, action_name, params, expected_call, monkeypatch
):
    hass, entry = _both_native_action_harness()
    _pin_registry_bindings(monkeypatch, hass)
    params = {"vehicle_vin": VIN, "charger_type": "tesla", **params}
    _pin_solar_ownership(monkeypatch, action_name)
    monkeypatch.setattr(
        actions, "_tesla_transport_authority", lambda *args, **kwargs: lambda: True
    )
    monkeypatch.setattr(actions, "_tesla_transport_route", lambda *args, **kwargs: _async_value(None))
    monkeypatch.setattr(actions, "_resolve_ble_prefix_for_vehicle", lambda *args: "ble")
    monkeypatch.setattr(actions, "_is_ble_available", lambda *args: False)
    monkeypatch.setattr(actions, "_resolve_teslemetry_bt_prefix_for_vehicle", lambda *args: None)
    monkeypatch.setattr(actions, "_is_teslemetry_bt_available", lambda *args: False)
    monkeypatch.setattr(actions, "_get_tesla_ev_entity", lambda *args, **kwargs: _async_value(None))
    monkeypatch.setattr(actions, "_is_api_credit_available", lambda *args: True)
    monkeypatch.setattr(actions, "_wake_tesla_ev_for_command", lambda *args, **kwargs: _async_value(True))
    monkeypatch.setattr(actions, "prepare_ev_submission", lambda *args, **kwargs: _async_value(True))
    modern_dispatch = AsyncMock(side_effect=AssertionError("modern command dispatched"))
    dispatch = importlib.import_module("power_sync.tesla_ev_dispatch")
    monkeypatch.setattr(dispatch, "async_dispatch_bluetooth_command", modern_dispatch)
    legacy_calls = []
    for name in (
        "_start_ev_charging_teslemetry_bt",
        "_stop_ev_charging_teslemetry_bt",
        "_set_ev_charging_amps_teslemetry_bt",
    ):
        monkeypatch.setattr(
            actions,
            name,
            AsyncMock(side_effect=lambda *args, _name=name, **kwargs: legacy_calls.append(_name)),
        )

    result = await getattr(actions, action_name)(hass, entry, params)

    assert result is True
    assert modern_dispatch.await_count == 0
    assert legacy_calls == []
    assert hass.services.calls == [expected_call]
    assert params["_tesla_native_router_selected"] is True
    assert params["_tesla_native_router_target"].vin == VIN


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected"),
    (("rate", "number.native_current"), ("limit", "number.native_limit")),
)
async def test_quarantined_native_anchor_keeps_rate_and_limit_on_same_provider(
    operation, expected, monkeypatch
):
    hass, entry = _both_native_action_harness()
    _pin_registry_bindings(monkeypatch, hass)
    params = {"vehicle_vin": VIN, "charger_type": "tesla"}
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    monkeypatch.setattr(
        recovery,
        "get_vehicle_route",
        lambda *_args, **_kwargs: {"kind": "ha", "entity_id": "switch.native_charge"},
    )

    assert await actions._tesla_ha_command_entity(
        hass, entry, params, operation
    ) == expected


@pytest.mark.asyncio
async def test_unknown_start_adopts_only_fresh_native_charging(modern, monkeypatch):
    hass, entry, params, target, current, finished = modern
    current["outcome"] = DispatchOutcome.SENT_UNKNOWN
    observation = _observation("Charging", current=8, power=4)
    monkeypatch.setattr(
        actions, "_fresh_tesla_command_observation",
        lambda *args, **kwargs: _async_value(observation),
    )

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start") == (True, True)
    assert params["_tesla_command_entity_id"] == target.entity_id
    assert params["_tesla_start_observation"] is observation
    assert params["_tesla_start_live_read_required"] is True
    assert "_tesla_transport_recovery_pending" not in params
    # Physical readback adopts the effect but cannot fence an already queued
    # command-only packet, so the durable producer hazard remains unknown.
    assert [getattr(value, "value", value) for value in finished] == ["sent_unknown"]


@pytest.mark.asyncio
async def test_accepted_start_without_fresh_physical_readback_has_no_hazard(modern, monkeypatch):
    hass, entry, params, _target_value, current, finished = modern
    current["outcome"] = DispatchOutcome.ACCEPTED_UNCONFIRMED
    monkeypatch.setattr(
        actions, "_fresh_tesla_command_observation",
        lambda *args, **kwargs: _async_value(_observation("Stopped", current=0, power=0)),
    )

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start") == (True, None)
    assert "_tesla_transport_recovery_pending" not in params
    assert [getattr(value, "value", value) for value in finished] == [
        "accepted_unconfirmed"
    ]


@pytest.mark.asyncio
async def test_unknown_start_retains_hazard_without_fresh_physical_draw(modern, monkeypatch):
    hass, entry, params, _target_value, current, finished = modern
    current["outcome"] = DispatchOutcome.SENT_UNKNOWN
    monkeypatch.setattr(
        actions, "_fresh_tesla_command_observation",
        lambda *args, **kwargs: _async_value(_observation("Stopped", current=0, power=0)),
    )

    assert await actions._run_modern_teslemetry_bluetooth(
        hass, entry, params, "start") == (True, None)
    assert params["_tesla_transport_recovery_pending"] is True
    assert [getattr(value, "value", value) for value in finished] == ["sent_unknown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud_recovery", [False, True])
async def test_unknown_start_journal_blocks_repeated_commands_and_paid_reads(monkeypatch, cloud_recovery):
    """An unresolved command cannot spend again through a repeated adapter call."""
    hass, entry = _both_native_action_harness()
    _pin_registry_bindings(monkeypatch, hass)
    entry.options = {"ev_provider": (
        actions.EV_PROVIDER_BOTH if cloud_recovery else actions.EV_PROVIDER_TESLEMETRY_BT
    )}
    if cloud_recovery:
        # A native read source can exist without an enabled native Start
        # entity; in that case Both uses the separate Bluetooth command.
        del hass.entity_registry.entities["switch.native_charge"]
    commands = importlib.import_module("power_sync.teslemetry_bluetooth_commands")
    dispatch = importlib.import_module("power_sync.tesla_ev_dispatch")
    recovery = importlib.import_module("power_sync.tesla_ev_recovery")
    observation = importlib.import_module("power_sync.tesla_ev_observation")
    # Keep the real journal and source checks; replace only durable storage
    # and the external read/command boundaries with deterministic fakes.
    monkeypatch.setattr(recovery, "_persist", AsyncMock(return_value=True))
    monkeypatch.setattr(recovery, "_registry", lambda _hass: hass.entity_registry)
    monkeypatch.setattr(actions, "prepare_ev_submission", _allow_submission)
    target = commands.resolve_command(hass, VIN, "start")
    assert target is not None
    submitted = []

    async def unknown_dispatch(_hass, resolved, **kwargs):
        if not await kwargs["before_dispatch"]():
            return DispatchReceipt(DispatchOutcome.NOT_SENT, resolved, reason="journal blocked")

        async def send():
            submitted.append(resolved.entity_id)

        await kwargs["submit"](send)
        return DispatchReceipt(DispatchOutcome.SENT_UNKNOWN, resolved,
                               submitted_at=time.time(), reason="provider timed out")

    monkeypatch.setattr(dispatch, "async_dispatch_bluetooth_command", unknown_dispatch)
    normal_read = AsyncMock(return_value=None)
    paid_read = AsyncMock(return_value=None)
    monkeypatch.setattr(observation, "async_read_tesla_vehicle", normal_read)
    monkeypatch.setattr(observation, "async_read_independent_tesla_vehicle", paid_read)
    monkeypatch.setattr(observation, "independent_tesla_source_binding", lambda *args, **kwargs: {
        "domain": "teslemetry", "config_entry_id": "native-entry",
        "device_id": "teslemetry-device",
    })
    # A Bluetooth-only configuration forbids paid cloud recovery. With Both,
    # the first unknown attempt may read once, but the journal must prevent
    # the next twenty submissions from reaching another command or read.
    params = {"vehicle_vin": VIN}
    assert await actions._run_modern_teslemetry_bluetooth(hass, entry, params, "start") == (True, None)
    for _ in range(20):
        assert await actions._run_modern_teslemetry_bluetooth(hass, entry, params, "start") == (True, False)
    assert submitted == [target.entity_id]
    assert normal_read.await_count == 1
    assert paid_read.await_count == int(cloud_recovery)
    if cloud_recovery:
        assert paid_read.await_args.kwargs["allow_paid_refresh"] is True
    assert params["_tesla_transport_recovery_pending"] is True

    # Compose the outer recovery action too. Its five-minute retry may spend
    # one authorized read to discover that charging began after a timeout;
    # the unresolved journal must still block a second writer. This is a
    # recovery read, distinct from repeatedly submitting the same Start.
    from datetime import datetime, timedelta

    _pin_solar_ownership(monkeypatch, "_action_start_ev_charging")
    params["_boost_start_deadline"] = time.time() + 3600
    state = {"active": True, "params": params, "current_amps": 9,
             "transport_recovery_pending": True,
             "last_physical_restart_attempt_at": datetime.now().astimezone()}
    monkeypatch.setitem(actions._dynamic_ev_state, entry.entry_id, {VIN: state})
    for retry in range(1, 4):
        assert not actions._tesla_dynamic_restart_due(state)
        state["last_physical_restart_attempt_at"] -= timedelta(seconds=300)
        assert actions._tesla_dynamic_restart_due(state)
        assert await actions._restart_owned_tesla(hass, entry, VIN, state, 9) is False
        assert normal_read.await_count == 1 + retry
        assert paid_read.await_count == (1 + retry) * int(cloud_recovery)
        assert submitted == [target.entity_id]
        assert state["params"]["_boost_start_deadline"] == params["_boost_start_deadline"]
    assert hass.services.calls == []


@pytest.mark.asyncio
async def test_exception_after_handoff_retains_unknown_attempt(modern):
    hass, entry, params, _target_value, current, finished = modern
    current["outcome"] = RuntimeError("queued")

    with pytest.raises(RuntimeError, match="queued"):
        await actions._run_modern_teslemetry_bluetooth(
            hass, entry, params, "start")
    assert [getattr(value, "value", value) for value in finished] == ["sent_unknown"]


async def _async_value(value):
    await asyncio.sleep(0)
    return value
