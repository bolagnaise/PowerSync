"""Bounded live reads from the native HA Tesla vehicle providers."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from importlib import import_module
import inspect
import math
import re
import time
from typing import Any, Callable, Mapping


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
    origin: str = "native_coordinator"
    field_provenance: Mapping[str, str] = field(default_factory=dict)


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
    provider_identifiers: frozenset[tuple[str, str]]

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
    in_flight: dict[tuple[Any, ...], asyncio.Task["_SourceRefresh | None"]] = field(
        default_factory=dict
    )
    independent_in_flight: dict[tuple[Any, ...], asyncio.Task["TeslaVehicleObservation | None"]] = field(default_factory=dict)
    independent_timestamps: dict[tuple[Any, ...], float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _SourceRefresh:
    """The result of one bounded coordinator refresh."""

    started_at: float
    before_timestamp: float | None
    data: Any
    successful: bool


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


def _source_charge_timestamp(data: Any) -> float | None:
    return _timestamp(_charge_state(data).get("timestamp"))


def _fleet_state(data: Any) -> str | None:
    """Return the normalized Fleet vehicle state from one coordinator report."""
    if isinstance(data, Mapping):
        value = data.get("state")
    else:
        value = getattr(data, "state", None)
    if value is None:
        return None
    value = getattr(value, "value", value)
    state = str(value or "").strip().upper()
    return state or None


def _entry_id(entry: Any) -> str | None:
    value = getattr(entry, "entry_id", None)
    return str(value) if value else None


def _identifiers(value: Any) -> tuple[tuple[str, str], ...]:
    identifiers = (
        value.get("identifiers", ())
        if isinstance(value, Mapping)
        else getattr(value, "identifiers", ())
    )
    if isinstance(identifiers, Mapping):
        identifiers = identifiers.keys()
    result = []
    for identifier in identifiers or ():
        try:
            if isinstance(identifier, (str, bytes)):
                continue
            parts = tuple(identifier)
        except (TypeError, ValueError):
            continue
        if len(parts) != 2:
            continue
        domain, value = parts
        result.append((str(domain), str(value)))
    return tuple(result)


def _provider_identifiers(value: Any, provider: str) -> frozenset[tuple[str, str]]:
    """Return the provider's two-part device identifiers.

    Some integrations add unrelated identifiers with three or more parts.
    ``_identifiers`` deliberately ignores those entries so they cannot make a
    valid native source fail identity matching or trigger unpacking errors.
    """
    provider_key = str(provider).strip().lower()
    return frozenset(
        (domain.strip().lower(), identifier.strip().upper())
        for domain, identifier in _identifiers(value)
        if domain.strip().lower() == provider_key
    )


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


def _registered_device(hass: Any, device_id: str | None) -> Any | None:
    """Resolve one device-registry object without broad source fallbacks."""
    if not device_id:
        return None
    registry = _device_registry(hass)
    if registry is None:
        return None
    getter = getattr(registry, "async_get", None)
    if callable(getter):
        try:
            registered = getter(device_id)
        except (AttributeError, KeyError, TypeError):
            registered = None
        if registered is not None:
            return registered
    devices = getattr(registry, "devices", {})
    if isinstance(devices, Mapping):
        return devices.get(device_id)
    for registered in devices or ():
        if str(getattr(registered, "id", "") or "") == str(device_id):
            return registered
    return None


def _fleet_command_binding(hass: Any, entity_id: str) -> tuple[str, str, str, str] | None:
    """Return a complete active Fleet command registration identity."""
    registration = _entity_registration(hass, entity_id)
    if registration is None or getattr(registration, "disabled_by", object()) is not None:
        return None
    values = (
        getattr(registration, "platform", None),
        getattr(registration, "config_entry_id", None),
        getattr(registration, "device_id", None),
        getattr(registration, "unique_id", None),
    )
    if any(value is None or not str(value).strip() for value in values):
        return None
    platform, entry_id, device_id, unique_id = (str(value) for value in values)
    if platform != "tesla_fleet":
        return None
    return platform, entry_id, device_id, unique_id


def _fleet_command_source(
    hass: Any,
    vin: str,
    command_entity: str,
    binding: tuple[str, str, str, str],
) -> _Source | None:
    """Resolve exactly one Fleet source for an active command registration."""
    sources = _sources(hass, vin)
    matches = [
        source
        for source in sources
        if (
            source.domain == "tesla_fleet"
            and source.config_entry_id == binding[1]
            and source.device_id == binding[2]
        )
    ]
    if len(matches) != 1:
        return None
    source = matches[0]
    expected_provider_identifiers = frozenset({("tesla_fleet", vin)})
    if source.provider_identifiers != expected_provider_identifiers:
        return None
    registered = _registered_device(hass, binding[2])
    if (
        registered is None
        or _provider_identifiers(registered, "tesla_fleet")
        != expected_provider_identifiers
    ):
        return None
    selected = _select_source(hass, vin, command_entity)
    if selected is None or selected.key() != source.key():
        return None
    return source


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
        provider_identifiers = _provider_identifiers(
            getattr(vehicle, "device", None), domain
        )
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
                provider_identifiers=provider_identifiers,
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


async def _refresh_shared(
    hass: Any,
    source: _Source,
    *,
    timeout: float,
) -> _SourceRefresh | None:
    state = _read_state(hass)
    key = source.key()
    task = state.in_flight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(
            _refresh_source(
                source,
                timeout=timeout,
            )
        )
        state.in_flight[key] = task

        def cleanup(completed: asyncio.Task[_SourceRefresh | None]) -> None:
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


async def _read_shared(
    hass: Any,
    source: _Source,
    *,
    timeout: float,
    preferred_entity_id: str | None,
) -> TeslaVehicleObservation | None:
    refreshed = await _refresh_shared(hass, source, timeout=timeout)
    if refreshed is None or not refreshed.successful:
        return None
    if not _source_is_current(hass, source, preferred_entity_id):
        return None
    charge = _charge_state(refreshed.data)
    timestamp = _timestamp(charge.get("timestamp"))
    now = time.time()
    if timestamp is None or timestamp > now + 5 or now - timestamp > MAX_SOURCE_AGE_SECONDS:
        return None
    if timestamp < math.floor(refreshed.started_at):
        return None
    if refreshed.before_timestamp is not None and timestamp <= refreshed.before_timestamp:
        return None
    return _observation(source, charge, timestamp)


async def _refresh_source(
    source: _Source,
    *,
    timeout: float,
) -> _SourceRefresh | None:
    refresh = getattr(source.coordinator, "async_request_refresh", None)
    if not callable(refresh):
        return None
    before = _source_charge_timestamp(getattr(source.coordinator, "data", None))
    started = time.time()
    try:
        await asyncio.wait_for(refresh(), timeout=max(0.001, timeout))
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    return _SourceRefresh(
        started_at=started,
        before_timestamp=before,
        data=getattr(source.coordinator, "data", None),
        successful=getattr(source.coordinator, "last_update_success", False) is True,
    )


async def async_prepare_tesla_fleet_command(
    hass: Any,
    vin: str,
    command_entity: str,
    *,
    timeout: float = 10,
) -> bool:
    """Refresh the exact Fleet vehicle before a mutating command.

    Fleet command entities perform their own wake-up, but their wake decision
    is based on coordinator state.  A stale ``ONLINE`` state can therefore
    send a command directly to a sleeping vehicle.  Refresh one source bound
    to the command entity and allow the provider to own wake-up only when that
    refreshed source says ``ASLEEP`` or ``OFFLINE``.  A refreshed ``ONLINE``
    report must also carry a newer, fresh charge timestamp; cached or
    rate-limited data cannot authorize a write.
    """
    canonical = _canonical_vin(vin)
    if canonical is None:
        return False
    try:
        timeout_value = float(timeout)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(timeout_value) or timeout_value <= 0:
        return False
    timeout_value = min(timeout_value, MAX_TIMEOUT_SECONDS)
    try:
        binding = _fleet_command_binding(hass, command_entity)
        if binding is None:
            return False
        source = _fleet_command_source(hass, canonical, command_entity, binding)
    except Exception:
        return False
    if source is None:
        return False
    try:
        refreshed = await _refresh_shared(hass, source, timeout=timeout_value)
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    if refreshed is None or not refreshed.successful:
        return False
    try:
        if _fleet_command_binding(hass, command_entity) != binding:
            return False
        current_source = _fleet_command_source(hass, canonical, command_entity, binding)
    except Exception:
        return False
    if current_source is None or current_source.key() != source.key():
        return False
    state_value = _fleet_state(refreshed.data)
    if state_value in {"ASLEEP", "OFFLINE"}:
        return True
    if state_value != "ONLINE":
        return False
    timestamp = _source_charge_timestamp(refreshed.data)
    now = time.time()
    if timestamp is None or timestamp > now + 5 or now - timestamp > MAX_SOURCE_AGE_SECONDS:
        return False
    if timestamp < math.floor(refreshed.started_at):
        return False
    return refreshed.before_timestamp is None or timestamp > refreshed.before_timestamp


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


def _cloud_contract(source: _Source) -> tuple[Any, Any, Any] | None:
    """Resolve the inspected native cloud reader, separately from command APIs.

    HA 2026.10's Teslemetry vehicle may use a Bluetooth command router while
    its data coordinator retains a cloud Vehicle. Both native coordinators
    serialize REST refreshes with their debouncer's async_lock. Unknown client
    or coordinator implementations cannot attest an independent cloud read.
    """
    contracts = {
        "tesla_fleet": ("TeslaFleetVehicleDataCoordinator", "tesla_fleet_api.tesla", "VehicleFleet"),
        "teslemetry": ("TeslemetryVehicleDataCoordinator", "tesla_fleet_api.teslemetry", "Vehicle"),
    }
    contract = contracts.get(source.domain)
    if contract is None:
        return None
    try:
        coordinator_name, api_module, api_name = contract
        coordinator_class = getattr(import_module(f"homeassistant.components.{source.domain}.coordinator"), coordinator_name)
        api_class = getattr(import_module(api_module), api_name)
        endpoint = import_module("tesla_fleet_api.const").VehicleDataEndpoint.CHARGE_STATE
        api = source.coordinator.api
        debouncer = source.coordinator._debounced_refresh
        if (type(source.coordinator) is not coordinator_class
                or type(api) is not api_class
                or not callable(getattr(api_class, "vehicle_data", None))
                or not callable(getattr(debouncer, "async_lock", None))):
            return None
        return api, debouncer, endpoint
    except (ImportError, AttributeError, TypeError):
        return None


def _independent_identity(hass: Any, source: _Source, preferred: str | None) -> tuple[Any, ...] | None:
    current = _select_source(hass, source.vin, preferred)
    if (current is None or current.key() != source.key()
            or not current.config_entry_id or not current.device_id
            or current.provider_identifiers != frozenset({(source.domain, source.vin)})):
        return None
    registration = None
    if preferred:
        entity = _entity_registration(hass, preferred)
        if entity is None or getattr(entity, "disabled_by", object()) is not None:
            return None
        registration = tuple(getattr(entity, name, None) for name in (
            "platform", "config_entry_id", "device_id", "unique_id"))
        if any(not isinstance(value, str) or not value for value in registration):
            return None
    return current.key(), frozenset(current.device_identifiers), current.provider_identifiers, registration


def independent_tesla_source_binding(
    hass: Any, vin: str, *, preferred_entity_id: str | None = None,
) -> dict[str, Any] | None:
    """Pin the current native cloud reader without performing provider I/O.

    The token is local runtime identity, never a firmware nonce or persisted
    historical identity. Callers must compare it again after their awaits.
    """
    canonical = _canonical_vin(vin)
    source = _select_source(hass, canonical, preferred_entity_id) if canonical else None
    if source is None:
        return None
    contract = _cloud_contract(source)
    identity = _independent_identity(hass, source, preferred_entity_id)
    if contract is None or identity is None:
        return None
    return {
        "vin": canonical, "domain": source.domain,
        "config_entry_id": source.config_entry_id, "device_id": source.device_id,
        "producer_identity": source.producer_identity,
        "source_token": (identity, id(contract[0]), id(contract[1])),
    }


async def async_wake_independent_tesla_vehicle(
    hass: Any,
    vin: str,
    *,
    preferred_entity_id: str | None = None,
    expected_binding: Mapping[str, Any] | None = None,
    authorize: Callable[[], bool] | None = None,
    timeout: float = 60,
) -> bool:
    """Request one exact native-cloud vehicle wake.

    This is deliberately separate from the command router.  Teslemetry's
    vehicle wrapper can route commands through its BLE adapter, so resolving
    the configured coordinator's concrete cloud API class and calling its
    class method is required for an independent wake.  The wake acknowledgement
    is only a provider request; callers must perform a later independent read
    before releasing a recovery incident.
    """
    canonical = _canonical_vin(vin)
    timeout_value = _finite(timeout, minimum=0)
    if (canonical is None or timeout_value is None or timeout_value <= 0
            or timeout_value > 60):
        return False
    source = _select_source(hass, canonical, preferred_entity_id)
    if source is None:
        return False
    contract = _cloud_contract(source)
    binding = independent_tesla_source_binding(
        hass, canonical, preferred_entity_id=preferred_entity_id,
    )
    if contract is None or binding is None:
        return False
    if expected_binding is not None and dict(expected_binding) != binding:
        return False
    api, debouncer, _endpoint = contract
    wake_up = getattr(type(api), "wake_up", None)
    if not callable(wake_up):
        return False

    def current() -> bool:
        if authorize is not None:
            try:
                if not authorize():
                    return False
            except Exception:
                return False
        current_source = _select_source(hass, canonical, preferred_entity_id)
        current_contract = _cloud_contract(current_source) if current_source else None
        return bool(
            current_source is not None
            and current_source.key() == source.key()
            and current_contract is not None
            and current_contract[0] is api
            and current_contract[1] is debouncer
            and independent_tesla_source_binding(
                hass, canonical, preferred_entity_id=preferred_entity_id,
            ) == binding
        )

    async def wake() -> Any:
        async with debouncer.async_lock():
            if not current():
                return None
            result = wake_up(api, wait=False, timeout=timeout_value)
            if inspect.isawaitable(result):
                result = await result
            return result

    async def wait_until_online(response: Any) -> bool:
        """Poll only the exact cloud vehicle-info endpoint after POST wake."""
        vehicle = getattr(type(api), "vehicle", None)
        if not callable(vehicle):
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_value

        def online(value: Any) -> bool:
            if not isinstance(value, Mapping):
                return False
            payload = value.get("response")
            if not isinstance(payload, Mapping):
                return False
            return str(payload.get("state", "")).strip().lower() == "online"

        while True:
            if online(response):
                return current()
            remaining = deadline - loop.time()
            if remaining <= 0 or not current():
                return False
            await asyncio.sleep(min(5.0, remaining))
            if not current():
                return False
            async with debouncer.async_lock():
                if not current():
                    return False
                response = vehicle(api)
                if inspect.isawaitable(response):
                    response = await response
            if not current():
                return False

    try:
        async def wake_and_wait() -> bool:
            result = await wake()
            return (
                result is not None
                and result is not False
                and await wait_until_online(result)
            )

        if not await asyncio.wait_for(wake_and_wait(), timeout=timeout_value):
            return False
    except asyncio.CancelledError:
        raise
    except Exception:
        return False
    if not current():
        return False
    return True


async def async_read_independent_tesla_vehicle(
    hass: Any,
    vin: str,
    *,
    preferred_entity_id: str | None = None,
    timeout: float = 10,
    updated_after: Any | None = None,
) -> TeslaVehicleObservation | None:
    """Read one exact cloud charge-state response using the native HA client.

    This explicit REST read uses the selected coordinator's existing cloud API
    and refresh lock, never the vehicle command router or published stream
    cache. Every returned field belongs to this single charge_state document
    and its native timestamp. Requested current, limit, state, actual current
    and power must all be present; pilot/draw never substitute for requested
    current. Each completed read must advance the last independent timestamp.
    It does not wake, command, publish coordinator data, or change provider.
    """
    canonical = _canonical_vin(vin)
    cutoff = None if updated_after is None else _timestamp(updated_after)
    if canonical is None or (updated_after is not None and cutoff is None):
        return None
    timeout_value = _finite(timeout, minimum=0)
    if timeout_value is None or timeout_value <= 0:
        return None
    timeout_value = min(timeout_value, MAX_TIMEOUT_SECONDS)
    source = _select_source(hass, canonical, preferred_entity_id)
    if source is None:
        return None
    contract = _cloud_contract(source)
    identity = _independent_identity(hass, source, preferred_entity_id)
    if contract is None or identity is None:
        return None
    api, debouncer, endpoint = contract
    state = _read_state(hass)
    key = (*source.key(), id(api), id(debouncer))

    def current() -> bool:
        current_contract = _cloud_contract(source)
        return (
            current_contract is not None
            and current_contract[0] is api
            and current_contract[1] is debouncer
            and not getattr(source.coordinator, "_shutdown_requested", False)
            and not getattr(debouncer, "_shutdown_requested", False)
            and _independent_identity(hass, source, preferred_entity_id) == identity
        )

    async def read_cloud() -> TeslaVehicleObservation | None:
        async def locked_read() -> TeslaVehicleObservation | None:
            async with debouncer.async_lock():
                if not current():
                    return None
                before = _source_charge_timestamp(getattr(source.coordinator, "data", None))
                previous = state.independent_timestamps.get(key)
                started = time.time()
                # Call the known cloud class method on the existing client;
                # an instance command router override is never selected.
                response = await type(api).vehicle_data(api, endpoints=[endpoint])
                if not current() or not isinstance(response, Mapping):
                    return None
                data = response.get("response")
                if not isinstance(data, Mapping) or _canonical_vin(data.get("vin")) != canonical:
                    return None
                charge = _charge_state(deepcopy(data))
                stamp = _timestamp(charge.get("timestamp"))
                now = time.time()
                if (stamp is None or stamp < math.floor(started)
                        or stamp > now + 5 or now - stamp > MAX_SOURCE_AGE_SECONDS
                        or (before is not None and stamp <= before)
                        or (previous is not None and stamp <= previous)):
                    return None
                result = _observation(source, charge, stamp)
                if (result is None or result.charge_state.lower() not in {
                            "charging", "stopped", "complete", "disconnected", "starting", "nopower"}
                        or result.requested_current is None
                        or result.charge_limit_soc is None or result.charge_limit_soc > 100
                        or result.charger_actual_current is None or result.charger_power is None):
                    return None
                requested_key = "charge_current_request" if charge.get("charge_current_request") is not None else "charger_current_request"
                state.independent_timestamps[key] = stamp
                return TeslaVehicleObservation(
                    vin=result.vin, domain=result.domain,
                    config_entry_id=result.config_entry_id, device_id=result.device_id,
                    producer_identity=result.producer_identity,
                    source_charge_timestamp=stamp, charge_state=result.charge_state,
                    charger_actual_current=result.charger_actual_current,
                    charger_power=result.charger_power, charge_limit_soc=result.charge_limit_soc,
                    requested_current=result.requested_current, raw_charge_state=charge,
                    origin="native_cloud",
                    field_provenance={
                        "charge_state": "charge_state.charging_state",
                        "requested_current": f"charge_state.{requested_key}",
                        "charge_limit_soc": "charge_state.charge_limit_soc",
                        "charger_actual_current": "charge_state.charger_actual_current",
                        "charger_power": "charge_state.charger_power",
                    },
                )
        try:
            return await asyncio.wait_for(locked_read(), timeout=timeout_value)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None

    task = state.independent_in_flight.get(key)
    if task is None or task.done():
        task = asyncio.create_task(read_cloud())
        state.independent_in_flight[key] = task

        def cleanup(completed: asyncio.Task[TeslaVehicleObservation | None]) -> None:
            if state.independent_in_flight.get(key) is completed:
                state.independent_in_flight.pop(key, None)
            try:
                completed.exception()
            except BaseException:
                pass

        task.add_done_callback(cleanup)
    try:
        result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout_value)
    except asyncio.TimeoutError:
        return None
    if not current() or result is None or (cutoff is not None and result.source_charge_timestamp <= cutoff):
        return None
    return result


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
    "async_prepare_tesla_fleet_command",
    "async_read_tesla_vehicle",
    "async_read_independent_tesla_vehicle",
    "async_wake_independent_tesla_vehicle",
    "independent_tesla_source_binding",
    "get_tesla_vehicle_observation",
]
