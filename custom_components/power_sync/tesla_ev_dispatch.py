"""Fail-closed dispatch and cached readback for Teslemetry Bluetooth commands."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import inspect
import math
import time
from collections.abc import Mapping
from typing import Any, Awaitable, Callable

from .tesla_ev_observation import TeslaVehicleObservation, get_tesla_vehicle_observation
from .teslemetry_bluetooth_commands import BluetoothCommandTarget, resolve_command


class DispatchOutcome(str, Enum):
    NOT_SENT = "not_sent"
    SENT_UNKNOWN = "sent_unknown"
    ACCEPTED_UNCONFIRMED = "accepted_unconfirmed"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    outcome: DispatchOutcome
    target: BluetoothCommandTarget | None
    submitted_at: float | None = None
    observation: TeslaVehicleObservation | None = None
    reason: str | None = None


Gate = Callable[[], bool | Awaitable[bool]] | bool
MAX_READBACK_TIMEOUT = 30.0
STOP_TERMINAL_STATES = frozenset({"stopped", "complete", "disconnected"})


def _receipt(outcome, target, *, submitted_at=None, observation=None, reason):
    return DispatchReceipt(outcome, target, submitted_at, observation, reason)


def _not_sent(target, reason: str) -> DispatchReceipt:
    return _receipt(DispatchOutcome.NOT_SENT, target, reason=reason)


def _unknown(target, submitted_at: float, reason: str, observation=None) -> DispatchReceipt:
    return _receipt(DispatchOutcome.SENT_UNKNOWN, target, submitted_at=submitted_at,
                    observation=observation, reason=reason)


def _accepted_unconfirmed(
    target, submitted_at: float, reason: str, observation=None
) -> DispatchReceipt:
    return _receipt(
        DispatchOutcome.ACCEPTED_UNCONFIRMED,
        target,
        submitted_at=submitted_at,
        observation=observation,
        reason=reason,
    )


def _service_error_outcome(error: BaseException) -> DispatchOutcome:
    """Classify the wrapped tesla-fleet-api failure without guessing text."""
    cause = error
    seen: set[int] = set()
    while isinstance(cause, BaseException) and id(cause) not in seen:
        seen.add(id(cause))
        cls = type(cause)
        module = str(getattr(cls, "__module__", ""))
        name = str(getattr(cls, "__name__", ""))
        if module.startswith("tesla_fleet_api"):
            if name == "BluetoothTransportError":
                return DispatchOutcome.NOT_SENT
            if name == "BluetoothCommandFailed":
                return DispatchOutcome.REJECTED
            if name in {"BluetoothTimeout", "BluetoothUnconfirmedCommand"}:
                return DispatchOutcome.SENT_UNKNOWN
        cause = cause.__cause__
    return DispatchOutcome.SENT_UNKNOWN


def _exact_target(hass: Any, target: Any) -> BluetoothCommandTarget | None:
    if not isinstance(target, BluetoothCommandTarget):
        return None
    try:
        current = resolve_command(hass, target.vin, target.operation)
    except Exception:
        return None
    return target if current == target else None


def _number_limits(hass: Any, target: BluetoothCommandTarget) -> tuple[float, float]:
    """Read live number limits; 48 A is only the upstream fallback."""
    minimum, maximum = 0.0, 48.0
    states = getattr(hass, "states", None)
    getter = getattr(states, "get", None)
    try:
        state = getter(target.entity_id) if callable(getter) else None
    except Exception:
        state = None
    attrs = getattr(state, "attributes", {}) if state is not None else {}
    sources = [attrs]
    registry = getattr(hass, "entity_registry", None)
    get_entity = getattr(registry, "async_get", None)
    try:
        entity = get_entity(target.entity_id) if callable(get_entity) else None
    except Exception:
        entity = None
    sources.append(getattr(entity, "capability_attributes", None) or {})
    sources.append({key: getattr(entity, key, None) for key in (
        "min", "max", "min_value", "max_value", "native_min_value", "native_max_value"
    )})
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for keys, fallback in ((
            (("min", "min_value", "native_min_value"), "minimum"),
            (("max", "max_value", "native_max_value"), "maximum"),
        )):
            for key in keys:
                try:
                    candidate = float(source[key])
                except (KeyError, TypeError, ValueError, OverflowError):
                    continue
                if math.isfinite(candidate):
                    if fallback == "minimum":
                        minimum = candidate
                    else:
                        maximum = candidate
                    break
    return minimum, maximum


def _valid_value(hass: Any, target: BluetoothCommandTarget, value: Any) -> bool:
    if target.operation in ("start", "stop"):
        return value is None
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(number):
        return False
    if target.operation == "rate":
        minimum, maximum = _number_limits(hass, target)
        return minimum <= number <= maximum
    if target.operation == "limit":
        return 50 <= number <= 100
    return False


def _positive(value: Any) -> bool:
    try:
        return value is not None and math.isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError, OverflowError):
        return False


async def _gate(gate: Gate) -> bool:
    result = gate() if callable(gate) else gate
    if inspect.isawaitable(result):
        result = await result
    return result is True


def _command_call(target: BluetoothCommandTarget, value: Any) -> tuple[str, dict[str, Any]]:
    if target.operation in ("start", "stop"):
        return "press", {"entity_id": target.entity_id}
    return "set_value", {"entity_id": target.entity_id, "value": float(value)}


def _proof(
    target: BluetoothCommandTarget, value: Any, observation: TeslaVehicleObservation,
    submitted_at: float,
) -> bool:
    if (
        observation.vin != target.vin
        or observation.domain != "teslemetry"
        or observation.device_id != target.device_id
        or observation.config_entry_id not in target.native_config_entry_ids
        or observation.source_charge_timestamp <= submitted_at
    ):
        return False
    if target.operation == "start":
        return observation.charge_state.strip().lower() == "charging" and any(
            _positive(value)
            for value in (observation.charger_actual_current, observation.charger_power)
        )
    if target.operation == "stop":
        measurements = (observation.charger_actual_current, observation.charger_power)
        return (observation.charge_state.strip().lower() in STOP_TERMINAL_STATES
                and any(value == 0 for value in measurements if value is not None)
                and not any(_positive(value) for value in measurements))
    measured = (
        observation.requested_current
        if target.operation == "rate"
        else observation.charge_limit_soc
    )
    try:
        return measured is not None and float(measured) == float(value)
    except (TypeError, ValueError, OverflowError):
        return False


async def async_dispatch_bluetooth_command(
    hass: Any, target: BluetoothCommandTarget | None, *, value: Any = None,
    authorize: Gate, before_dispatch: Gate, readback_timeout: float = 10,
) -> DispatchReceipt:
    """Dispatch one exact command and confirm it from a fresh native report."""
    exact = _exact_target(hass, target)
    if exact is None:
        known_target = target if isinstance(target, BluetoothCommandTarget) else None
        return _not_sent(known_target, "target_replaced_or_invalid")
    if not _valid_value(hass, exact, value):
        return _not_sent(exact, "invalid_command_value")
    try:
        timeout = float(readback_timeout)
    except (TypeError, ValueError, OverflowError):
        timeout = 0
    if not math.isfinite(timeout) or timeout <= 0:
        return _not_sent(exact, "invalid_readback_timeout")
    timeout = min(timeout, MAX_READBACK_TIMEOUT)
    try:
        if not await _gate(authorize):
            return _not_sent(exact, "unauthorized")
        if not await _gate(before_dispatch):
            return _not_sent(exact, "before_dispatch_rejected")
        if _exact_target(hass, exact) is None or not await _gate(authorize):
            return _not_sent(exact, "target_or_authority_changed")
        if not _valid_value(hass, exact, value):
            return _not_sent(exact, "command_value_changed")
    except asyncio.CancelledError:
        raise
    except Exception as err:
        return _not_sent(exact, f"preflight_error:{type(err).__name__}")

    try:
        before_observation = get_tesla_vehicle_observation(
            hass, exact.vin, preferred_entity_id=exact.entity_id
        )
    except Exception:
        before_observation = None
    before_timestamp = (
        before_observation.source_charge_timestamp
        if before_observation is not None
        else None
    )
    submitted_at = time.time()
    service, data = _command_call(exact, value)
    try:
        await hass.services.async_call(exact.entity_domain, service, data, blocking=True)
    except asyncio.CancelledError:
        raise
    except BaseException as err:
        if isinstance(err, (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        outcome = _service_error_outcome(err)
        reason = f"service_error:{type(err).__name__}"
        if outcome is DispatchOutcome.NOT_SENT:
            return _not_sent(exact, reason)
        if outcome is DispatchOutcome.REJECTED:
            return _receipt(
                DispatchOutcome.REJECTED,
                exact,
                submitted_at=submitted_at,
                reason=reason,
            )
        return _unknown(exact, submitted_at, reason)

    deadline = time.monotonic() + timeout
    while True:
        try:
            if _exact_target(hass, exact) is None or not await _gate(authorize):
                return _accepted_unconfirmed(
                    exact, submitted_at, "target_or_authority_changed_after_dispatch"
                )
            # A whole-second timestamp may precede this command. Wait for a
            # strictly newer source sample; the acknowledged command remains
            # separate from an unconfirmed physical effect meanwhile.
            cutoff = submitted_at
            if before_timestamp is not None:
                cutoff = max(cutoff, before_timestamp)
            observation = get_tesla_vehicle_observation(
                hass, exact.vin, preferred_entity_id=exact.entity_id, updated_after=cutoff
            )
            if observation is not None and _proof(exact, value, observation, submitted_at):
                if _exact_target(hass, exact) is None or not await _gate(authorize):
                    return _accepted_unconfirmed(
                        exact, submitted_at,
                        "target_or_authority_changed_after_readback", observation,
                    )
                return _receipt(
                    DispatchOutcome.CONFIRMED,
                    exact,
                    submitted_at=submitted_at,
                    observation=observation,
                    reason="fresh_native_readback",
                )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            return _accepted_unconfirmed(
                exact, submitted_at, f"readback_error:{type(err).__name__}"
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _accepted_unconfirmed(
                exact, submitted_at, "fresh_native_readback_timeout"
            )
        await asyncio.sleep(min(0.1, remaining))


__all__ = ["DispatchOutcome", "DispatchReceipt", "async_dispatch_bluetooth_command"]
