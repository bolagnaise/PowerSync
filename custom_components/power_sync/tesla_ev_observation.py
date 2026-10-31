"""Bounded live reads from the native HA Tesla vehicle providers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import re
import time
from typing import Any, Mapping


VIN_PATTERN = re.compile(r"[A-HJ-NPR-Z0-9]{17}")
MAX_SOURCE_AGE_SECONDS = 90.0
MAX_TIMEOUT_SECONDS = 30.0
_STATE_DATA_KEY = "power_sync.tesla_ev_observation"


@dataclass(frozen=True, slots=True)
class TeslaVehicleObservation:
    """A source-linked Tesla charge-state observation."""

    vin: str
    domain: str
    config_entry_id: str | None
    device_id: str | None
    producer_identity: str
    source_charge_timestamp: float
    charge_state: str
    charger_actual_current: float | None
    charger_power: float | None
    charge_limit_soc: float | None
    requested_current: float | None
    raw_charge_state: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Source:
    domain: str
    vin: str
    entry: Any
    vehicle: Any
    coordinator: Any
    config_entry_id: str | None
    device_id: str | None
    producer_identity: str
    device_identifiers: tuple[tuple[str, str], ...]

    def key(self) -> tuple[Any, ...]:
        return (
            self.domain,
            self.vin,
            self.config_entry_id,
            self.device_id,
            id(self.entry),
            id(self.vehicle),
            id(self.coordinator),
        )


@dataclass
class _ReadState:
    in_flight: dict[tuple[Any, ...], asyncio.Task[TeslaVehicleObservation | None]] = field(
        default_factory=dict
    )


def _canonical_vin(value: Any) -> str | None:
    vin = str(value or "").strip().upper()
    return vin if VIN_PATTERN.fullmatch(vin) else None


def _finite(value: Any, *, minimum: float | None = None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        return None
    return number


def _timestamp(value: Any) -> float | None:
    stamp = _finite(value, minimum=0)
    if stamp is None:
        return None
    return stamp / 1000 if stamp > 100_000_000_000 else stamp


def _charge_state(data: Any) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        return {}
    nested = data.get("charge_state")
    if isinstance(nested, Mapping):
        return dict(nested)
    prefix = "charge_state_"
    return {
        str(key)[len(prefix) :]: value
        for key, value in data.items()
        if str(key).startswith(prefix)
    }


def _entry_id(entry: Any) -> str | None:
    value = getattr(entry, "entry_id", None)
    return str(value) if value else None


def _identifiers(value: Any) -> tuple[tuple[str, str], ...]:
    identifiers = getattr(value, "identifiers", ())
    if isinstance(identifiers, Mapping):
        identifiers = identifiers.keys()
    result = []
    for identifier in identifiers or ():
        try:
            domain, value = identifier
        except (TypeError, ValueError):
            continue
        result.append((str(domain), str(value)))
    return tuple(result)


def _device_registry(hass: Any) -> Any | None:
    try:
        from homeassistant.helpers import device_registry as dr

        return dr.async_get(hass)
    except (ImportError, AttributeError, TypeError):
        return getattr(hass, "device_registry", None)


def _device_identity(
    hass: Any,
    vehicle: Any,
    domain: str,
    vin: str,
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    device = getattr(vehicle, "device", None)
    identifiers = _identifiers(device) or ((domain, vin),)
    device_id = getattr(device, "id", None)
    device_id = str(device_id) if device_id else None
    try:
        registry = _device_registry(hass)
        devices = getattr(registry, "devices", {})
        devices = devices.values() if isinstance(devices, Mapping) else devices
        wanted = set(identifiers)
        for registered in devices:
            if wanted.intersection(_identifiers(registered)):
                registered_id = getattr(registered, "id", None)
                if registered_id:
                    device_id = str(registered_id)
                    break
    except (ImportError, AttributeError, TypeError):
        pass
    return device_id, identifiers


def _entity_registration(hass: Any, entity_id: str | None) -> Any | None:
    if not entity_id:
        return None
    try:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(hass).async_get(entity_id)
    except (ImportError, AttributeError, TypeError):
        registry = getattr(hass, "entity_registry", None)
        getter = getattr(registry, "async_get", None)
        return getter(entity_id) if callable(getter) else None


def _sources(hass: Any, vin: str) -> list[_Source]:
    try:
        from .native_rate_telemetry import vehicle_sources
        matches = vehicle_sources(hass, vin)
    except (ImportError, AttributeError, TypeError):
        # Discovery belongs to native_rate_telemetry.  A missing provider
        # module is an unavailable source, never a reason to inspect entries
        # through a second implementation here.
        return []

    sources = []
    for domain, entry, vehicle, coordinator in matches:
        if str(getattr(vehicle, "vin", "")).upper() != vin:
            continue
        device_id, identifiers = _device_identity(hass, vehicle, domain, vin)
        entry_id = _entry_id(entry)
        sources.append(
            _Source(
                domain=str(domain),
                vin=vin,
                entry=entry,
                vehicle=vehicle,
                coordinator=coordinator,
                config_entry_id=entry_id,
                device_id=device_id,
                producer_identity=f"{domain}:{entry_id or ''}:{device_id or ''}:{vin}",
                device_identifiers=identifiers,
            )
        )
    # Native Teslemetry retains its own BLE/cloud router.  Keep one selected
    # source and do not silently switch providers after a failed read.
    return sorted(sources, key=lambda source: source.domain != "teslemetry")


def _preferred_source(hass: Any, sources: list[_Source], entity_id: str) -> _Source | None:
    registration = _entity_registration(hass, entity_id)
    if registration is None:
        return None
    preferred_platform = str(getattr(registration, "platform", "") or "")
    preferred_entry = getattr(registration, "config_entry_id", None)
    preferred_device = getattr(registration, "device_id", None)
    preferred_device = str(preferred_device) if preferred_device else None

    # Native provider entities must resolve within the same provider and
    # config entry.  This prevents a same-VIN Fleet entry from winning merely
    # because a device registry happened to reuse an identifier.
    for source in sources:
        if (
            preferred_platform == source.domain
            and preferred_entry
            and source.config_entry_id == str(preferred_entry)
            and (preferred_device is None or source.device_id == preferred_device)
        ):
            return source

    # Teslemetry Bluetooth Commands is a separate command-only integration.
    # Its entities attach to the native Teslemetry device, so a command entity
    # can identify the native source by shared device identity.  It must never
    # redirect to Fleet when both providers expose the VIN.
    if preferred_platform != "teslemetry_bluetooth" or not preferred_device:
        return None
    try:
        registry = _device_registry(hass)
        getter = getattr(registry, "async_get", None)
        registered = getter(preferred_device) if callable(getter) else None
    except (AttributeError, TypeError):
        registered = None
    registered_identifiers = _identifiers(registered)
    for source in sources:
        if source.domain != "teslemetry":
            continue
        if source.device_id == preferred_device:
            return source
        if registered_identifiers and set(source.device_identifiers).intersection(
            registered_identifiers
        ):
            return source
    return None


def _select_source(hass: Any, vin: str, preferred_entity_id: str | None) -> _Source | None:
    sources = _sources(hass, vin)
    if preferred_entity_id:
        return _preferred_source(hass, sources, preferred_entity_id)
    return sources[0] if sources else None


def _source_is_current(hass: Any, selected: _Source, preferred_entity_id: str | None) -> bool:
    current = _select_source(hass, selected.vin, preferred_entity_id)
    return current is not None and current.key() == selected.key()


def _observation(
    source: _Source,
    charge: Mapping[str, Any],
    timestamp: float,
) -> TeslaVehicleObservation | None:
    state = str(charge.get("charging_state") or "").strip()
    if not state:
        return None
    requested = charge.get("charge_current_request")
    if requested is None:
        requested = charge.get("charger_current_request")
    return TeslaVehicleObservation(
        vin=source.vin,
        domain=source.domain,
        config_entry_id=source.config_entry_id,
        device_id=source.device_id,
        producer_identity=source.producer_identity,
        source_charge_timestamp=timestamp,
        charge_state=state,
        charger_actual_current=_finite(charge.get("charger_actual_current"), minimum=0),
        charger_power=_finite(charge.get("charger_power"), minimum=0),
        charge_limit_soc=_finite(charge.get("charge_limit_soc"), minimum=0),
        requested_current=_finite(requested, minimum=0),
        raw_charge_state=dict(charge),
    )


def _cached_observation(
    hass: Any,
    source: _Source,
    *,
    preferred_entity_id: str | None,
    updated_after: float | None,
) -> TeslaVehicleObservation | None:
    """Parse one already-published coordinator report without requesting data."""
    if getattr(source.coordinator, "last_update_success", False) is not True:
        return None
    if not _source_is_current(hass, source, preferred_entity_id):
        return None
    charge = _charge_state(getattr(source.coordinator, "data", None))
    timestamp = _timestamp(charge.get("timestamp"))
    now = time.time()
    if timestamp is None or timestamp > now + 5 or now - timestamp > MAX_SOURCE_AGE_SECONDS:
        return None
    if updated_after is not None and timestamp <= updated_after:
        return None
    return _observation(source, charge, timestamp)


async def _read_source(
    hass: Any,
    source: _Source,
    *,
    timeout: float,
    preferred_entity_id: str | None,
) -> TeslaVehicleObservation | None:
    refresh = getattr(source.coordinator, "async_request_refresh", None)
    if not callable(refresh):
        return None
    before = _timestamp(_charge_state(getattr(source.coordinator, "data", None)).get("timestamp"))
    started = time.time()
    try:
        await asyncio.wait_for(refresh(), timeout=max(0.001, timeout))
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    if getattr(source.coordinator, "last_update_success", False) is not True:
        return None
    if not _source_is_current(hass, source, preferred_entity_id):
        return None
    charge = _charge_state(getattr(source.coordinator, "data", None))
    timestamp = _timestamp(charge.get("timestamp"))
    now = time.time()
    if timestamp is None or timestamp > now + 5 or now - timestamp > MAX_SOURCE_AGE_SECONDS:
        return None
    # API timestamps are often whole seconds.  A changed timestamp in the
    # request's current second is acceptable; an unchanged cached timestamp is
    # never accepted, even when HA reported the coordinator as updated.
    if timestamp < math.floor(started):
        return None
    if before is not None and timestamp <= before:
        return None
    return _observation(source, charge, timestamp)


def _read_state(hass: Any) -> _ReadState:
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return _ReadState()
    state = data.get(_STATE_DATA_KEY)
    if isinstance(state, _ReadState):
        return state
    state = _ReadState()
    data[_STATE_DATA_KEY] = state
    return state


async def _read_shared(
    hass: Any,
    source: _Source,
    *,
    timeout: float,
    preferred_entity_id: str | None,
) -> TeslaVehicleObservation | None:
    state = _read_state(hass)
    key = source.key()
    task = state.in_flight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _read_source(
                hass,
                source,
                timeout=timeout,
                preferred_entity_id=preferred_entity_id,
            )
        )
        state.in_flight[key] = task

        def cleanup(completed: asyncio.Task[TeslaVehicleObservation | None]) -> None:
            if state.in_flight.get(key) is completed:
                state.in_flight.pop(key, None)
            try:
                completed.exception()
            except BaseException:
                pass

        task.add_done_callback(cleanup)
    try:
        # Shield keeps cancellation of one waiter from cancelling the shared
        # provider request used by another waiter.
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, timeout))
    except asyncio.TimeoutError:
        return None


async def async_read_tesla_vehicle(
    hass: Any,
    vin: str,
    *,
    preferred_entity_id: str | None = None,
    timeout: float = 10,
) -> TeslaVehicleObservation | None:
    """Read exactly one native provider source for ``vin``.

    This performs one explicit coordinator refresh only.  It never wakes the
    vehicle, invokes a control command, starts recurring polling, or falls
    back to another provider after the selected source fails.
    """
    canonical = _canonical_vin(vin)
    if canonical is None:
        return None
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        return None
    timeout_value = min(timeout_value, MAX_TIMEOUT_SECONDS)
    source = _select_source(hass, canonical, preferred_entity_id)
    if source is None:
        return None
    return await _read_shared(
        hass,
        source,
        timeout=timeout_value,
        preferred_entity_id=preferred_entity_id,
    )


def get_tesla_vehicle_observation(
    hass: Any,
    vin: str,
    *,
    preferred_entity_id: str | None = None,
    updated_after: Any | None = None,
) -> TeslaVehicleObservation | None:
    """Return a fresh cached native report without causing provider I/O.

    ``updated_after`` is compared only with the provider's vehicle
    ``charge_state.timestamp``.  HA entity receipt times and ``last_reported``
    values cannot satisfy this fence.
    """
    canonical = _canonical_vin(vin)
    if canonical is None:
        return None
    cutoff = None if updated_after is None else _timestamp(updated_after)
    if updated_after is not None and cutoff is None:
        return None
    source = _select_source(hass, canonical, preferred_entity_id)
    if source is None:
        return None
    return _cached_observation(
        hass,
        source,
        preferred_entity_id=preferred_entity_id,
        updated_after=cutoff,
    )


__all__ = [
    "TeslaVehicleObservation",
    "async_read_tesla_vehicle",
    "get_tesla_vehicle_observation",
]
