"""Scheduled HACS auto-update support for PowerSync."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.update import UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
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
HACS_REFRESH_RETRIES = 3
HACS_REFRESH_INTERVAL_S = 60
# Width of the trigger window after the configured time. Lets a late integration
# reload (e.g. options-update reload landing at 03:00:30) still pick up the day's
# run on the next minute-tick instead of waiting until tomorrow. Bounded so HA
# boots later in the day don't surprise users with a daytime restart.
TRIGGER_WINDOW_HOURS = 4
LAST_RUN_STORE_VERSION = 1
POWER_SYNC_UPDATE_HINTS = (
    "power_sync",
    "powersync",
    "power sync",
    "tesla_amber_sync",
    "tesla amber sync",
)
AUTO_UPDATE_ACTION_INSTALLED = "installed"
AUTO_UPDATE_ACTION_PENDING_RESTART = "pending_restart"


@dataclass(frozen=True)
class AutoUpdateInstallResult:
    """Outcome that requires Home Assistant to restart."""

    entity_id: str
    action: str


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


def _version_is_newer(latest: Any, installed: Any) -> bool:
    """Return True when latest appears newer than installed."""
    if not latest or not installed:
        return False

    latest_text = str(latest).strip().lstrip("v")
    installed_text = str(installed).strip().lstrip("v")
    if not latest_text or not installed_text or latest_text == installed_text:
        return False

    try:
        from packaging.version import Version

        return Version(latest_text) > Version(installed_text)
    except Exception:
        pass

    try:
        latest_parts = tuple(int(part) for part in latest_text.split("."))
        installed_parts = tuple(int(part) for part in installed_text.split("."))
    except ValueError:
        return latest_text != installed_text

    return latest_parts > installed_parts


def _state_has_pending_update(state: Any) -> bool:
    """Return True when an update state should be installed."""
    if state.state == "on":
        return True

    attrs = state.attributes or {}
    return _version_is_newer(
        attrs.get("latest_version"),
        attrs.get("installed_version"),
    )


def _state_needs_restart(state: Any) -> bool:
    """Return True when HACS has downloaded an update pending HA restart."""
    attrs = state.attributes or {}
    release_summary = str(attrs.get("release_summary", "")).lower()
    return "restart" in release_summary and "home assistant" in release_summary


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


async def _refresh_hacs_entities(hass: HomeAssistant, entity_ids: list[str]) -> None:
    """Best-effort refresh of HACS update entities."""
    try:
        await hass.services.async_call(
            HOMEASSISTANT_DOMAIN,
            SERVICE_UPDATE_ENTITY,
            {ATTR_ENTITY_ID: entity_ids},
            blocking=True,
        )
    except Exception as err:
        _LOGGER.debug("PowerSync auto-update: HACS entity refresh raised: %s", err)


async def async_install_power_sync_update(
    hass: HomeAssistant,
    *,
    exclude_entity_id: str | None = None,
) -> AutoUpdateInstallResult | None:
    """Install the currently available PowerSync HACS update, if one exists.

    The update entity is HACS's supported Home Assistant boundary. Refresh it
    without calling HACS repository internals, which may be mid-initialization
    and must remain owned by HACS. Retry to give HACS's coordinator time to
    discover a release that was published near the scheduled run.
    """
    for attempt in range(1, HACS_REFRESH_RETRIES + 1):
        # Rediscover on every attempt. During startup/reload HACS can add its
        # update entity, or populate its supported features, after this
        # scheduler has already started.
        entity_ids = find_power_sync_update_entities(
            hass,
            require_install=True,
            exclude_entity_id=exclude_entity_id,
        )
        if not entity_ids:
            if attempt < HACS_REFRESH_RETRIES:
                _LOGGER.info(
                    "PowerSync auto-update: no install-capable HACS update "
                    "entity yet (attempt %d/%d) — waiting %ds before retry",
                    attempt,
                    HACS_REFRESH_RETRIES,
                    HACS_REFRESH_INTERVAL_S,
                )
                await asyncio.sleep(HACS_REFRESH_INTERVAL_S)
                continue
            _LOGGER.warning(
                "PowerSync auto-update: no install-capable HACS update entity "
                "found after %d attempts. Verify PowerSync is installed via HACS.",
                HACS_REFRESH_RETRIES,
            )
            return None

        _LOGGER.info(
            "PowerSync auto-update: candidate HACS update entities: %s",
            entity_ids,
        )
        await _refresh_hacs_entities(hass, entity_ids)

        for entity_id in entity_ids:
            state = hass.states.get(entity_id)
            if state is None:
                continue

            if _state_needs_restart(state):
                _LOGGER.info(
                    "PowerSync auto-update: %s is already pending a Home "
                    "Assistant restart",
                    entity_id,
                )
                return AutoUpdateInstallResult(
                    entity_id=entity_id,
                    action=AUTO_UPDATE_ACTION_PENDING_RESTART,
                )

            if _state_has_pending_update(state):
                _LOGGER.info(
                    "PowerSync auto-update: installing via %s "
                    "(attempt %d, installed=%s, latest=%s)",
                    entity_id,
                    attempt,
                    state.attributes.get("installed_version"),
                    state.attributes.get("latest_version"),
                )
                await hass.services.async_call(
                    UPDATE_DOMAIN,
                    SERVICE_INSTALL,
                    {ATTR_ENTITY_ID: entity_id},
                    blocking=True,
                )
                return AutoUpdateInstallResult(
                    entity_id=entity_id,
                    action=AUTO_UPDATE_ACTION_INSTALLED,
                )

        if attempt < HACS_REFRESH_RETRIES:
            _LOGGER.info(
                "PowerSync auto-update: HACS reports no update yet "
                "(attempt %d/%d) — waiting %ds before retry",
                attempt,
                HACS_REFRESH_RETRIES,
                HACS_REFRESH_INTERVAL_S,
            )
            await asyncio.sleep(HACS_REFRESH_INTERVAL_S)

    _LOGGER.info(
        "PowerSync auto-update: HACS reports no update available after "
        "%d refresh attempts",
        HACS_REFRESH_RETRIES,
    )
    return None


async def async_run_power_sync_auto_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
    last_run_store: Store,
) -> None:
    """Install a pending PowerSync HACS update and restart Home Assistant."""
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    now_local = dt_util.now()
    entry_data["auto_update_last_run"] = dt_util.utcnow().isoformat()

    # Persist before any heavy work so a reload mid-install (options change,
    # auto-detection, etc.) doesn't re-trigger the same day's run.
    try:
        await last_run_store.async_save(
            {"last_run_date": now_local.date().isoformat()}
        )
    except Exception as err:
        _LOGGER.warning(
            "PowerSync auto-update: failed to persist last_run_date: %s", err
        )

    try:
        result = await async_install_power_sync_update(hass)
    except Exception as err:
        _LOGGER.exception("PowerSync auto-update: install raised: %s", err)
        entry_data["auto_update_last_result"] = "error"
        return

    if not result:
        entry_data["auto_update_last_result"] = "no_update"
        return

    entry_data["auto_update_last_entity"] = result.entity_id
    entry_data["auto_update_last_result"] = result.action
    _LOGGER.info(
        "PowerSync auto-update: %s via %s; restarting Home Assistant in %d "
        "seconds",
        result.action,
        result.entity_id,
        AUTO_UPDATE_RESTART_DELAY,
    )
    await asyncio.sleep(AUTO_UPDATE_RESTART_DELAY)
    await hass.services.async_call(
        HOMEASSISTANT_DOMAIN,
        SERVICE_RESTART,
        blocking=False,
    )


async def async_setup_auto_update(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Callable[[], None]:
    """Set up the once-per-day auto-update scheduler.

    The schedule fires once per local day, anywhere within a TRIGGER_WINDOW_HOURS
    window starting at the configured time, as long as it hasn't already run
    that day. The "already ran today" flag is persisted to disk so that the
    integration's own option-update reload (which destroys the in-memory
    listener and creates a fresh closure) cannot cause double-fires within a
    day, and equally cannot cause a same-day skip when reload happens during
    the trigger minute.
    """
    last_run_store = Store(
        hass,
        LAST_RUN_STORE_VERSION,
        f"{DOMAIN}.auto_update_last_run.{entry.entry_id}",
    )

    persisted = await last_run_store.async_load()
    last_run_date: date | None = None
    if isinstance(persisted, dict):
        try:
            last_run_date = date.fromisoformat(persisted.get("last_run_date") or "")
        except (TypeError, ValueError):
            last_run_date = None

    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})

    def _record_check(decision: str) -> None:
        entry_data["auto_update_last_check_at"] = dt_util.utcnow().isoformat()
        entry_data["auto_update_last_check_decision"] = decision
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_{entry.entry_id}_auto_update_state",
        )

    @callback
    def _check_schedule(now: datetime) -> None:
        nonlocal last_run_date

        enabled = _entry_option(entry, CONF_AUTO_UPDATE_ENABLED, False)
        if not enabled:
            _record_check("disabled")
            return

        try:
            hour, minute = parse_auto_update_time(
                _entry_option(entry, CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME)
            )
        except ValueError:
            hour, minute = parse_auto_update_time(DEFAULT_AUTO_UPDATE_TIME)

        configured_min = hour * 60 + minute
        current_min = now.hour * 60 + now.minute

        if current_min < configured_min:
            _record_check("before_window")
            return
        if current_min >= configured_min + TRIGGER_WINDOW_HOURS * 60:
            _record_check("past_window")
            return
        if last_run_date == now.date():
            _record_check("already_ran_today")
            return

        last_run_date = now.date()
        _record_check("triggered")
        _LOGGER.info(
            "PowerSync auto-update: triggering install (configured=%02d:%02d, "
            "current=%s, last_run_date now=%s)",
            hour,
            minute,
            now.strftime("%H:%M:%S"),
            last_run_date,
        )
        hass.async_create_task(
            async_run_power_sync_auto_update(hass, entry, last_run_store),
            name=f"{DOMAIN}_auto_update",
        )

    _LOGGER.info(
        "PowerSync auto-update: scheduler registered "
        "(enabled=%s, time=%s, restored_last_run=%s, window_hours=%d)",
        _entry_option(entry, CONF_AUTO_UPDATE_ENABLED, False),
        _entry_option(entry, CONF_AUTO_UPDATE_TIME, DEFAULT_AUTO_UPDATE_TIME),
        last_run_date,
        TRIGGER_WINDOW_HOURS,
    )

    return async_track_time_change(hass, _check_schedule, second=0)
