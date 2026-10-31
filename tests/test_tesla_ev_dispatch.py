"""Focused tests for the fail-closed Teslemetry Bluetooth dispatcher."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
import sys
import time
import types

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync import tesla_ev_dispatch as dispatch  # noqa: E402
from power_sync.tesla_ev_observation import TeslaVehicleObservation  # noqa: E402
from power_sync.teslemetry_bluetooth_commands import BluetoothCommandTarget  # noqa: E402


VIN = "5YJ3E1EA7NF0000A1"


class BluetoothTransportError(BaseException):
    __module__ = "tesla_fleet_api.exceptions"


class BluetoothCommandFailed(BaseException):
    __module__ = "tesla_fleet_api.exceptions"


@dataclass
class _Services:
    calls: list[tuple]
    error: Exception | None = None

    async def async_call(self, domain, service, data, *, blocking):
        self.calls.append((domain, service, data, blocking))
        if self.error is not None:
            raise self.error


@dataclass
class _Hass:
    services: _Services


def _target(operation: str, suffix: str = "a") -> BluetoothCommandTarget:
    domain = "button" if operation in ("start", "stop") else "number"
    return BluetoothCommandTarget(
        vin=VIN,
        operation=operation,
        entity_id=f"{domain}.car_{suffix}_{operation}",
        entity_domain=domain,
        platform="teslemetry_bluetooth",
        unique_id=f"{VIN}-bluetooth-{operation}",
        config_entry_id="commands-entry",
        device_id="teslemetry-device",
        native_config_entry_ids=("native-entry",),
    )


def _observation(operation: str, value, *, timestamp=None) -> TeslaVehicleObservation:
    timestamp = timestamp or (time.time() + 1)
    state = "Charging" if operation == "start" else "Stopped"
    return TeslaVehicleObservation(
        vin=VIN,
        domain="teslemetry",
        config_entry_id="native-entry",
        device_id="teslemetry-device",
        producer_identity="teslemetry:native-entry:teslemetry-device:" + VIN,
        source_charge_timestamp=timestamp,
        charge_state=state,
        charger_actual_current=8 if operation == "start" else 0,
        charger_power=4 if operation == "start" else 0,
        charge_limit_soc=value if operation == "limit" else 80,
        requested_current=value if operation == "rate" else 16,
        raw_charge_state={"timestamp": timestamp},
    )


@pytest.fixture
def harness(monkeypatch):
    hass = _Hass(_Services([]))
    current = {"target": None, "observation": None}

    def resolve(_hass, vin, operation):
        target = current["target"]
        return target if target is not None and target.vin == vin and target.operation == operation else None

    monkeypatch.setattr(dispatch, "resolve_command", resolve)
    monkeypatch.setattr(
        dispatch,
        "get_tesla_vehicle_observation",
        lambda *args, **kwargs: current["observation"],
    )
    return hass, current


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,value", [("start", None), ("stop", None), ("rate", 16), ("limit", 90)])
async def test_all_operations_confirm_from_fresh_native_readback(harness, operation, value):
    hass, current = harness
    target = _target(operation)
    current["target"] = target
    current["observation"] = _observation(operation, value)

    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=value, authorize=lambda: True, before_dispatch=lambda: True
    )

    assert receipt.outcome is dispatch.DispatchOutcome.CONFIRMED
    assert receipt.observation is current["observation"]
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0][3] is True
    assert hass.services.calls[0][1] == ("press" if operation in ("start", "stop") else "set_value")


@pytest.mark.asyncio
async def test_unknown_number_entity_is_still_dispatchable(harness):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    current["observation"] = _observation("rate", 16)
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=16, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_rate_uses_live_car_maximum_above_upstream_fallback(harness):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    current["observation"] = _observation("rate", 80)
    hass.states = types.SimpleNamespace(
        get=lambda _entity_id: types.SimpleNamespace(attributes={"min": 0, "max": 80})
    )
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=80, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_rate_above_live_maximum_is_not_sent(harness):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    hass.states = types.SimpleNamespace(
        get=lambda _entity_id: types.SimpleNamespace(attributes={"min": 0, "max": 48})
    )
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=49, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.NOT_SENT
    assert not hass.services.calls


@pytest.mark.asyncio
async def test_definite_preflight_failure_is_not_sent(harness):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=-1, authorize=lambda: True, before_dispatch=lambda: True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.NOT_SENT
    assert not hass.services.calls


@pytest.mark.asyncio
async def test_stale_cached_readback_is_accepted_but_physical_unconfirmed(harness):
    hass, current = harness
    target = _target("stop")
    current["target"] = target
    current["observation"] = None
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True, readback_timeout=0.01
    )
    assert receipt.outcome is dispatch.DispatchOutcome.ACCEPTED_UNCONFIRMED
    assert receipt.submitted_at is not None
    assert len(hass.services.calls) == 1


@pytest.mark.asyncio
async def test_start_accepts_positive_power_with_rounded_zero_current(harness):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    current["observation"] = replace(_observation("start", None), charger_actual_current=0, charger_power=0.1)
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_same_second_integer_source_timestamp_does_not_confirm_physical_effect(
    harness, monkeypatch
):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    observation = _observation("start", None, timestamp=1000)
    reads = iter((None, observation))
    monkeypatch.setattr(dispatch.time, "time", lambda: 1000.2)
    monkeypatch.setattr(
        dispatch,
        "get_tesla_vehicle_observation",
        lambda *args, **kwargs: next(reads, observation),
    )

    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True, readback_timeout=0.001
    )

    assert receipt.outcome is dispatch.DispatchOutcome.ACCEPTED_UNCONFIRMED


@pytest.mark.asyncio
async def test_stop_accepts_one_real_zero_measurement_when_other_is_missing(harness):
    hass, current = harness
    target = _target("stop")
    current["target"] = target
    current["observation"] = replace(_observation("stop", None), charger_actual_current=0, charger_power=None)
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.CONFIRMED


@pytest.mark.asyncio
async def test_service_exception_after_handoff_is_unknown(harness):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    hass.services.error = RuntimeError("queued")
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.SENT_UNKNOWN
    assert receipt.reason == "service_error:RuntimeError"


@pytest.mark.asyncio
async def test_prewrite_transport_cause_is_not_sent(harness):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    error = RuntimeError("before write")
    error.__cause__ = BluetoothTransportError()
    hass.services.error = error
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.NOT_SENT


@pytest.mark.asyncio
async def test_proven_nonapplication_cause_is_rejected(harness):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    error = RuntimeError("rejected")
    error.__cause__ = BluetoothCommandFailed()
    hass.services.error = error
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.REJECTED


@pytest.mark.asyncio
async def test_target_change_in_before_callback_prevents_service(harness):
    hass, current = harness
    target = _target("stop", "a")
    current["target"] = target

    async def before_dispatch():
        current["target"] = _target("stop", "b")
        return True

    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, authorize=True, before_dispatch=before_dispatch
    )
    assert receipt.outcome is dispatch.DispatchOutcome.NOT_SENT
    assert not hass.services.calls


@pytest.mark.asyncio
async def test_owner_change_in_before_callback_prevents_service(harness):
    hass, current = harness
    target = _target("stop")
    current["target"] = target
    allowed = {"value": True}

    async def before_dispatch():
        allowed["value"] = False
        return True

    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass,
        target,
        authorize=lambda: allowed["value"],
        before_dispatch=before_dispatch,
    )
    assert receipt.outcome is dispatch.DispatchOutcome.NOT_SENT
    assert not hass.services.calls


@pytest.mark.asyncio
async def test_owner_or_target_change_during_readback_stays_unknown(harness, monkeypatch):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    current["observation"] = _observation("rate", 16)

    original = dispatch.get_tesla_vehicle_observation

    def changed(*args, **kwargs):
        current["target"] = _target("rate", "replacement")
        return original(*args, **kwargs)

    monkeypatch.setattr(dispatch, "get_tesla_vehicle_observation", changed)
    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=16, authorize=True, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.ACCEPTED_UNCONFIRMED
    assert len(hass.services.calls) == 1


@pytest.mark.asyncio
async def test_owner_change_after_fresh_readback_stays_unknown(harness):
    hass, current = harness
    target = _target("rate")
    current["target"] = target
    current["observation"] = _observation("rate", 16)
    calls = {"count": 0}

    def authorize():
        calls["count"] += 1
        return calls["count"] < 4

    receipt = await dispatch.async_dispatch_bluetooth_command(
        hass, target, value=16, authorize=authorize, before_dispatch=True
    )
    assert receipt.outcome is dispatch.DispatchOutcome.ACCEPTED_UNCONFIRMED
    assert receipt.reason == "target_or_authority_changed_after_readback"


@pytest.mark.asyncio
async def test_cancellation_before_callback_propagates_and_does_not_call_service(harness):
    hass, current = harness
    target = _target("start")
    current["target"] = target
    entered = asyncio.Event()
    release = asyncio.Event()

    async def before_dispatch():
        entered.set()
        await release.wait()
        return True

    task = asyncio.create_task(
        dispatch.async_dispatch_bluetooth_command(
            hass, target, authorize=True, before_dispatch=before_dispatch
        )
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not hass.services.calls
