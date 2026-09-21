"""Periodic Tesla Powerwall BMS health polling."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later, async_track_time_interval

_LOGGER = logging.getLogger(__name__)

POWERWALL_BMS_HEALTH_POLL_INTERVAL = timedelta(minutes=5)
# ``async_track_time_interval`` does not fire on arming, so without a prompt
# first poll the sensor publishes whatever the restore path put there for a
# full interval after every reload (Discord ticket 64).
POWERWALL_BMS_HEALTH_INITIAL_POLL_DELAY = timedelta(seconds=30)

BatteryHealthFetcher = Callable[[], Awaitable[dict[str, Any] | None]]
BatteryHealthSyncer = Callable[[dict[str, Any]], Awaitable[None]]


def async_start_powerwall_bms_health_polling(
    hass: HomeAssistant,
    entry_id: str,
    fetch: BatteryHealthFetcher,
    sync: BatteryHealthSyncer,
    *,
    interval: timedelta = POWERWALL_BMS_HEALTH_POLL_INTERVAL,
    initial_delay: timedelta = POWERWALL_BMS_HEALTH_INITIAL_POLL_DELAY,
) -> Callable[[], None]:
    """Poll BMS health periodically and publish successful samples to sensors."""
    in_progress = False

    async def _poll(now=None) -> None:
        nonlocal in_progress
        if in_progress:
            _LOGGER.debug("Skipping overlapping Powerwall BMS health poll for %s", entry_id)
            return

        in_progress = True
        try:
            payload = await fetch()
            if payload:
                await sync(payload)
        except Exception as err:
            _LOGGER.debug("Powerwall BMS health poll failed for %s: %s", entry_id, err)
        finally:
            in_progress = False

    cancel_interval = async_track_time_interval(hass, _poll, interval)
    cancel_initial = async_call_later(hass, initial_delay, _poll)

    def _cancel() -> None:
        cancel_initial()
        cancel_interval()

    return _cancel
