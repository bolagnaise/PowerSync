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
from collections.abc import Mapping
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


async def async_read_storage_state(
    hass: Any, command_entity_id: str
) -> dict[str, Any] | None:
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
            return None

        entry_id = getattr(entity, "config_entry_id", None)
        device_id = getattr(entity, "device_id", None)
        unique_id = getattr(entity, "unique_id", None)
        if not all(
            isinstance(value, str) and value
            for value in (entry_id, device_id, unique_id)
        ):
            return None
        if not unique_id.endswith(_COMMAND_SUFFIX):
            return None

        device = dr.async_get(hass).async_get(device_id)
        if device is None or entry_id not in set(getattr(device, "config_entries", ())):
            return None
        inverter_ids = {
            identifier[1]
            for identifier in getattr(device, "identifiers", ())
            if isinstance(identifier, tuple)
            and len(identifier) == 2
            and identifier[0] == _DOMAIN
            and isinstance(identifier[1], str)
        }
        if len(inverter_ids) != 1:
            return None
        inverter_uid = next(iter(inverter_ids))
        if unique_id != f"{inverter_uid}{_COMMAND_SUFFIX}":
            return None

        domain_data = getattr(hass, "data", {}).get(_DOMAIN)
        runtime = domain_data.get(entry_id) if isinstance(domain_data, Mapping) else None
        if not isinstance(runtime, Mapping):
            return None
        hub = runtime.get("hub")
        coordinator = runtime.get("coordinator")
        if hub is None or not isinstance(coordinator, TimestampDataUpdateCoordinator):
            return None
        if (
            getattr(coordinator, "_hub", None) is not hub
            or getattr(hub, "option_storage_control", None) is not True
            or getattr(hub, "has_write", None) is not None
        ):
            return None

        matches = [
            inverter
            for inverter in getattr(hub, "inverters", ())
            if getattr(inverter, "uid_base", None) == inverter_uid
        ]
        if len(matches) != 1:
            return None
        inverter = matches[0]
        previous_decoded = getattr(inverter, "decoded_storage_control", None)

        previous_success_time = getattr(coordinator, "last_update_success_time", None)
        if previous_success_time is None:
            return None
        refresh = getattr(coordinator, "async_request_refresh", None)
        add_listener = getattr(coordinator, "async_add_listener", None)
        if not callable(refresh) or not callable(add_listener):
            return None
        updated = asyncio.Event()
        notification_count = 0

        def _handle_update() -> None:
            nonlocal notification_count
            notification_count += 1
            updated.set()

        unsubscribe = add_listener(_handle_update)
        if not callable(unsubscribe):
            return None
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
                    return None
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
                    return None
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
                    return None
        finally:
            unsubscribe()

        decoded = getattr(inverter, "decoded_storage_control", None)
        if not isinstance(decoded, Mapping) or getattr(hub, "has_write", None) is not None:
            return None
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
        if any(key not in state for key in _REQUIRED_FIELDS):
            return None
        return state
    except (asyncio.CancelledError, KeyboardInterrupt):
        raise
    except Exception:
        _LOGGER.debug("SolarEdge storage readback unavailable", exc_info=True)
        return None


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
        "allow_grid_charge": "ac_charge_policy",
        "charge_power_limit": "charge_limit",
        "discharge_power_limit": "discharge_limit",
        "command_timeout": "command_timeout",
        "backup_reserve": "backup_reserve",
    }
    return {key: state[raw_key] for key, raw_key in fields.items() if raw_key in state}


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
        return value <= 86400
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
