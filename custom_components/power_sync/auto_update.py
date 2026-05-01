"""Scheduled HACS auto-update support for PowerSync."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_UPDATE_ENABLED,
    CONF_AUTO_UPDATE_TIME,
    DEFAULT_AUTO_UPDATE_TIME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

HOMEASSISTANT_DOMAIN = "homeassistant"
UPDATE_DOMAIN = "update"
SERVICE_INSTALL = "install"
SERVICE_RESTART = "restart"
SERVICE_UPDATE_ENTITY = "update_entity"
AUTO_UPDATE_RESTART_DELAY = 60
POWER_SYNC_UPDATE_HINTS = (
    "power_sync",
    "powersync",
    "power sync",
    "tesla_amber_sync",
    "tesla amber sync",
)


def parse_auto_update_time(value: Any) -> tuple[int, int]:
    """Parse an HH:MM or HH:MM:SS time string."""
    text = str(value or DEFAULT_AUTO_UPDATE_TIME).strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError("time must be HH:MM or HH:MM:SS")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(parts[2]) if len(parts) == 3 else 0
    if (
        hour < 0
        or hour > 23
        or minute < 0
        or minute > 59
        or second < 0
        or second > 59
    ):
        raise ValueError("time is out of range")
    return hour, minute


def normalize_auto_update_time(value: Any) -> str:
    """Return a normalized HH:MM time string."""
    hour, minute = parse_auto_update_time(value)
    return f"{hour:02d}:{minute:02d}"


def _entry_option(entry: ConfigEntry, key: str, default: Any = None) -> Any:
    """Read config entry options with data fallback."""
    return entry.options.get(key, entry.data.get(key, default))


def _state_haystack(state: Any) -> str:
    """Build searchable text for an update entity state."""
    attrs = state.attributes or {}
    values = [
        state.entity_id,
        attrs.get("friendly_name", ""),
        attrs.get("title", ""),
        attrs.get("release_url", ""),
        attrs.get("entity_picture", ""),
    ]
    return (
        " ".join(str(value) for value in values if value)
        .lower()
        .replace("-", "_")
    )


def _supports_install(state: Any) -> bool:
    """Return True when an update entity supports update.install."""
    try:
        supported = int(state.attributes.get("supported_features", 0))
    except (TypeError, ValueError):
        supported = 0
    return bool(supported & int(UpdateEntityFeature.INSTALL))


def find_power_sync_update_entities(
    hass: HomeAssistant,
    *,
    require_install: bool = True,
    exclude_entity_id: str | None = None,
) -> list[str]:
    """Find likely PowerSync HACS update entities."""
    matches: list[str] = []
    for state in hass.states.async_all(UPDATE_DOMAIN):
        if exclude_entity_id and state.entity_id == exclude_entity_id:
            continue
        haystack = _state_haystack(state)
        if not any(hint in haystack for hint in POWER_SYNC_UPDATE_HINTS):
            continue
        if require_install and not _supports_install(state):
            continue
        matches.append(state.entity_id)
    return matches


async def async_install_power_sync_update(
    hass: HomeAssistant,
    *,
    exclude_entity_id: str | None = None,
) -> str | None:
    """Install the currently available PowerSync HACS update, if one exists."""
    entity_ids = find_power_sync_update_entities(
        hass,
        require_install=True,
        exclude_entity_id=exclude_entity_id,
    )
    if not entity_ids:
        _LOGGER.warning(
            "PowerSync auto-update enabled, but no install-capable HACS update "
            "entity was found"
        )
        return None

    try:
        await hass.services.async_call(
            HOMEASSISTANT_DOMAIN,
            SERVICE_UPDATE_ENTITY,
            {ATTR_ENTITY_ID: entity_ids},
            blocking=True,
        )
    except Exception as err:
        _LOGGER.debug("PowerSync update entity refresh failed: %s", err)

    for entity_id in entity_ids:
        state = hass.states.get(entity_id)
        if state is None or state.state != "on":
            continue

        _LOGGER.info("Installing PowerSync update via %s", entity_id)
        await hass.services.async_call(
            UPDATE_DOMAIN,
            SERVICE_INSTALL,
            {ATTR_ENTITY_ID: entity_id},
            blocking=True,
        )
        return entity_id

    _LOGGER.debug(
        "PowerSync auto-update checked %s; no update is currently available",
        entity_ids,
    )
    return None


async def async_run_power_sync_auto_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Install a pending PowerSync HACS update and restart Home Assistant."""
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data["auto_update_last_run"] = dt_util.utcnow().isoformat()

    entity_id = await async_install_power_sync_update(hass)
    if not entity_id:
        entry_data["auto_update_last_result"] = "no_update"
        return

    entry_data["auto_update_last_entity"] = entity_id
    entry_data["auto_update_last_result"] = "installed"
    _LOGGER.info(
        "PowerSync update installed via %s; restarting Home Assistant in %d seconds",
        entity_id,
        AUTO_UPDATE_RESTART_DELAY,
    )
    await asyncio.sleep(AUTO_UPDATE_RESTART_DELAY)
    await hass.services.async_call(
        HOMEASSISTANT_DOMAIN,
        SERVICE_RESTART,
        blocking=False,
    )


def async_setup_auto_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Callable[[], None]:
    """Set up the once-per-day auto-update scheduler."""
    last_run_date: date | None = None

    def _check_schedule(now: datetime) -> None:
        nonlocal last_run_date

        enabled = _entry_option(entry, CONF_AUTO_UPDATE_ENABLED, False)
        if not enabled:
            return

        try:
            hour, minute = parse_auto_update_time(
                _entry_option(entry, CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME)
            )
        except ValueError:
            hour, minute = parse_auto_update_time(DEFAULT_AUTO_UPDATE_TIME)

        if now.hour != hour or now.minute != minute:
            return
        if last_run_date == now.date():
            return

        last_run_date = now.date()
        hass.async_create_task(
            async_run_power_sync_auto_update(hass, entry),
            name=f"{DOMAIN}_auto_update",
        )

    return async_track_time_change(hass, _check_schedule, second=0)
