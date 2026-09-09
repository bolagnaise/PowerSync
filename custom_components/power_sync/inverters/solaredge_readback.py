"""Guarded readback from SolarEdge Modbus Multi's completed poll data.

This module does not open a Modbus connection or call a Home Assistant service.
Its runtime contract was checked against SolarEdge Modbus Multi 3.3.9. It
accepts data only while the upstream identity, coordinator, enum maps, and
register shape still match that contract.
"""

from __future__ import annotations

import asyncio
import logging
import math
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_DOMAIN = "solaredge_modbus_multi"
_LOGGER = logging.getLogger(__name__)
_COMMAND_SUFFIX = "_storage_command_mode"
_REFRESH_TIMEOUT_SECONDS = 30.0
# Provisional pause between a closed poll connection and the caller's next write.
# Live validation must establish whether rapid session turnover causes timeouts.
_TRANSPORT_SETTLE_SECONDS = 2.0

_REQUIRED_FIELDS = (
    "control_mode",
    "command_mode",
    "command_timeout",
    "charge_limit",
    "discharge_limit",
    "default_mode",
)

_RAW_FIELDS = (
    "control_mode",
    "ac_charge_policy",
    "ac_charge_limit",
    "backup_reserve",
    "default_mode",
    "command_timeout",
    "command_mode",
    "charge_limit",
    "discharge_limit",
)


@dataclass(frozen=True)
class StorageReadbackResult:
    """Fresh decoded state or a safe diagnostic without register values."""

    state: dict[str, Any] | None
    reason: str
    fields: tuple[str, ...] = ()


async def async_read_storage_result(
    hass: Any, command_entity_id: str
) -> StorageReadbackResult:
    """Return one inverter's storage registers after a fresh upstream poll.

    Enum register values are returned using the same labels as the upstream HA
    select entities. Missing or invalid dispatch fields fail closed. Unsupported
    optional fields are omitted; callers must require every field they write.
    """
    try:
        from custom_components.solaredge_modbus_multi.const import (
            STORAGE_AC_CHARGE_POLICY,
            STORAGE_CONTROL_MODE,
            STORAGE_MODE,
        )
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers.update_coordinator import (
            TimestampDataUpdateCoordinator,
        )

        entity = er.async_get(hass).async_get(command_entity_id)
        if entity is None or getattr(entity, "platform", None) != _DOMAIN:
            return StorageReadbackResult(None, "identity_mismatch")

        entry_id = getattr(entity, "config_entry_id", None)
        device_id = getattr(entity, "device_id", None)
        unique_id = getattr(entity, "unique_id", None)
        if not all(
            isinstance(value, str) and value
            for value in (entry_id, device_id, unique_id)
        ):
            return StorageReadbackResult(None, "identity_mismatch")
        if not unique_id.endswith(_COMMAND_SUFFIX):
            return StorageReadbackResult(None, "identity_mismatch")

        device = dr.async_get(hass).async_get(device_id)
        if device is None or entry_id not in set(getattr(device, "config_entries", ())):
            return StorageReadbackResult(None, "identity_mismatch")
        inverter_ids = {
            identifier[1]
            for identifier in getattr(device, "identifiers", ())
            if isinstance(identifier, tuple)
            and len(identifier) == 2
            and identifier[0] == _DOMAIN
            and isinstance(identifier[1], str)
        }
        if len(inverter_ids) != 1:
            return StorageReadbackResult(None, "identity_mismatch")
        inverter_uid = next(iter(inverter_ids))
        if unique_id != f"{inverter_uid}{_COMMAND_SUFFIX}":
            return StorageReadbackResult(None, "identity_mismatch")

        domain_data = getattr(hass, "data", {}).get(_DOMAIN)
        runtime = (
            domain_data.get(entry_id) if isinstance(domain_data, Mapping) else None
        )
        if not isinstance(runtime, Mapping):
            return StorageReadbackResult(None, "readback_unavailable")
        hub = runtime.get("hub")
        coordinator = runtime.get("coordinator")
        if hub is None or not isinstance(coordinator, TimestampDataUpdateCoordinator):
            return StorageReadbackResult(None, "readback_unavailable")
        if (
            getattr(coordinator, "_hub", None) is not hub
            or getattr(hub, "option_storage_control", None) is not True
        ):
            return StorageReadbackResult(None, "readback_unavailable")

        if getattr(hub, "has_write", None) is not None:
            return StorageReadbackResult(None, "upstream_write_busy")

        matches = [
            inverter
            for inverter in getattr(hub, "inverters", ())
            if getattr(inverter, "uid_base", None) == inverter_uid
        ]
        if len(matches) != 1:
            return StorageReadbackResult(None, "identity_mismatch")
        inverter = matches[0]
        previous_decoded = getattr(inverter, "decoded_storage_control", None)

        previous_success_time = getattr(coordinator, "last_update_success_time", None)
        if previous_success_time is None:
            return _stale_result(hub)
        refresh = getattr(coordinator, "async_request_refresh", None)
        add_listener = getattr(coordinator, "async_add_listener", None)
        if not callable(refresh) or not callable(add_listener):
            return _stale_result(hub)
        updated = asyncio.Event()
        notification_count = 0

        def _handle_update() -> None:
            nonlocal notification_count
            notification_count += 1
            updated.set()

        unsubscribe = add_listener(_handle_update)
        if not callable(unsubscribe):
            return _stale_result(hub)
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _REFRESH_TIMEOUT_SECONDS
            await asyncio.wait_for(refresh(), timeout=_remaining(loop, deadline))
            while not _fresh_storage_read(
                coordinator,
                hub,
                inverter,
                previous_success_time,
                previous_decoded,
            ):
                if notification_count:
                    return _stale_result(hub)
                updated.clear()
                if _fresh_storage_read(
                    coordinator,
                    hub,
                    inverter,
                    previous_success_time,
                    previous_decoded,
                ):
                    break
                if notification_count:
                    return _stale_result(hub)
                await asyncio.wait_for(
                    updated.wait(), timeout=_remaining(loop, deadline)
                )
            if (
                getattr(hub, "keep_modbus_open", None) is False
                and getattr(hub, "is_connected", None) is False
            ):
                await asyncio.wait_for(
                    asyncio.sleep(_TRANSPORT_SETTLE_SECONDS),
                    timeout=_remaining(loop, deadline),
                )
                if not _fresh_storage_read(
                    coordinator,
                    hub,
                    inverter,
                    previous_success_time,
                    previous_decoded,
                ):
                    return _stale_result(hub)
        finally:
            unsubscribe()

        # Refresh and transport settling can yield across an integration reload.
        # The old coordinator's completed poll must not validate a new binding.
        current_domain = getattr(hass, "data", {}).get(_DOMAIN)
        if not isinstance(current_domain, Mapping) or entry_id not in current_domain:
            return StorageReadbackResult(None, "readback_unavailable")
        if (
            current_domain.get(entry_id) is not runtime
            or runtime.get("hub") is not hub
            or runtime.get("coordinator") is not coordinator
            or getattr(coordinator, "_hub", None) is not hub
        ):
            return StorageReadbackResult(None, "identity_mismatch")
        current_entity = er.async_get(hass).async_get(command_entity_id)
        if current_entity is None or (
            getattr(current_entity, "platform", None),
            getattr(current_entity, "config_entry_id", None),
            getattr(current_entity, "device_id", None),
            getattr(current_entity, "unique_id", None),
        ) != (_DOMAIN, entry_id, device_id, unique_id):
            return StorageReadbackResult(None, "identity_mismatch")
        current_device = dr.async_get(hass).async_get(device_id)
        if current_device is None or entry_id not in set(
            getattr(current_device, "config_entries", ())
        ):
            return StorageReadbackResult(None, "identity_mismatch")
        current_ids = {
            identifier[1]
            for identifier in getattr(current_device, "identifiers", ())
            if isinstance(identifier, tuple)
            and len(identifier) == 2
            and identifier[0] == _DOMAIN
            and isinstance(identifier[1], str)
        }
        current_matches = [
            candidate
            for candidate in getattr(hub, "inverters", ())
            if getattr(candidate, "uid_base", None) == inverter_uid
        ]
        if (
            current_ids != {inverter_uid}
            or len(current_matches) != 1
            or current_matches[0] is not inverter
        ):
            return StorageReadbackResult(None, "identity_mismatch")
        if getattr(hub, "option_storage_control", None) is not True:
            return StorageReadbackResult(None, "readback_unavailable")

        decoded = getattr(inverter, "decoded_storage_control", None)
        if getattr(hub, "has_write", None) is not None:
            return StorageReadbackResult(None, "upstream_write_busy")
        if not isinstance(decoded, Mapping):
            return StorageReadbackResult(None, "malformed_snapshot")
        enum_maps = {
            "control_mode": STORAGE_CONTROL_MODE,
            "ac_charge_policy": STORAGE_AC_CHARGE_POLICY,
            "default_mode": STORAGE_MODE,
            "command_mode": STORAGE_MODE,
        }
        state = {}
        for key in _RAW_FIELDS:
            value = decoded.get(key)
            if key in enum_maps:
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
                value = enum_maps[key].get(value)
                if not isinstance(value, str) or not value:
                    continue
            elif not _valid_numeric_field(key, value):
                continue
            state[key] = value
        if "control_mode" not in state:
            return StorageReadbackResult(None, "malformed_snapshot", ("control_mode",))
        native = (
            decoded["control_mode"] == 1
            and state["control_mode"] == "Maximize Self Consumption"
        )
        remote = (
            decoded["control_mode"] == 4 and state["control_mode"] == "Remote Control"
        )
        invalid_optional = tuple(
            key
            for key in ("ac_charge_policy", "ac_charge_limit", "backup_reserve")
            if key not in state and not _inapplicable_field(key, decoded.get(key))
        )
        if invalid_optional:
            return StorageReadbackResult(None, "malformed_snapshot", invalid_optional)
        if native:
            invalid = tuple(
                key
                for key in _REQUIRED_FIELDS[1:]
                if key not in state and not _inapplicable_field(key, decoded.get(key))
            )
            if invalid:
                return StorageReadbackResult(None, "malformed_snapshot", invalid)
            for key in _REQUIRED_FIELDS[1:]:
                state.pop(key, None)
        elif remote:
            invalid = tuple(key for key in _REQUIRED_FIELDS if key not in state)
            if invalid:
                return StorageReadbackResult(None, "malformed_snapshot", invalid)
        else:
            return StorageReadbackResult(
                None, "unsupported_storage_mode", ("control_mode",)
            )
        return StorageReadbackResult(state, "fresh_upstream_storage_poll")
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except TimeoutError:
        return StorageReadbackResult(None, "readback_not_fresh")
    except Exception:
        _LOGGER.debug("SolarEdge storage readback unavailable", exc_info=True)
        return StorageReadbackResult(None, "readback_unavailable")


async def async_read_storage_state(
    hass: Any, command_entity_id: str
) -> dict[str, Any] | None:
    """Return fresh decoded fields, omitting native-mode remote registers."""
    return (await async_read_storage_result(hass, command_entity_id)).state


async def async_read_storage_baseline(
    hass: Any, command_entity_id: str
) -> dict[str, Any] | None:
    """Return the fresh register snapshot using controller field names."""
    state = await async_read_storage_state(hass, command_entity_id)
    if state is None:
        return None
    fields = {
        "storage_control_mode": "control_mode",
        "storage_command_mode": "command_mode",
        "storage_default_mode": "default_mode",
        "allow_grid_charge": "ac_charge_policy",
        "charge_power_limit": "charge_limit",
        "discharge_power_limit": "discharge_limit",
        "command_timeout": "command_timeout",
        "backup_reserve": "backup_reserve",
    }
    return {key: state[raw_key] for key, raw_key in fields.items() if raw_key in state}


def _stale_result(hub: Any) -> StorageReadbackResult:
    reason = (
        "upstream_write_busy"
        if getattr(hub, "has_write", None) is not None
        else "readback_not_fresh"
    )
    return StorageReadbackResult(None, reason)


def _inapplicable_field(key: str, value: Any) -> bool:
    """Recognize only absent values and documented register sentinels."""
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if key in {"command_mode", "default_mode", "ac_charge_policy"}:
        return isinstance(value, int) and value == 0xFFFF
    if key == "command_timeout":
        return isinstance(value, int) and value == 0xFFFFFFFF
    if key in {
        "charge_limit",
        "discharge_limit",
        "ac_charge_limit",
        "backup_reserve",
    } and isinstance(value, float):
        return (
            math.isnan(value)
            and struct.unpack("<I", struct.pack("<f", value))[0] == 0x7FC00000
        )
    return False


def _valid_numeric_field(key: str, value: Any) -> bool:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value < 0
    ):
        return False
    if key == "backup_reserve":
        return value <= 100
    if key == "command_timeout":
        return isinstance(value, int) and value <= 86400
    if key in {"charge_limit", "discharge_limit"}:
        return value <= 1_000_000
    return True


def _fresh_storage_read(
    coordinator: Any,
    hub: Any,
    inverter: Any,
    previous_success_time: Any,
    previous_decoded: Any,
) -> bool:
    current_success_time = getattr(coordinator, "last_update_success_time", None)
    return (
        getattr(hub, "has_write", None) is None
        and getattr(coordinator, "last_update_success", None) is True
        and current_success_time is not None
        and current_success_time > previous_success_time
        and getattr(inverter, "decoded_storage_control", None) is not previous_decoded
    )


def _remaining(loop: asyncio.AbstractEventLoop, deadline: float) -> float:
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError
    return remaining
