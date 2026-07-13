"""PowerSync Cloud energy-flow reporter.

Opt-in "Report energy flow to PowerSync Cloud" feature. Every
DEFAULT_CLOUD_FLOW_INTERVAL seconds, reads the user's configured grid/solar/
battery/load entities from Home Assistant and POSTs a ChargeHQ-compatible
payload to POWERSYNC_FLOW_API_URL, so PowerSync Cloud can drive
charge-on-solar decisions for battery-less / non-Tesla-energy-site accounts
(the `ha_flow_reporter` feature-flag rollout).

`build_payload()` is a pure function (no Home Assistant object required) so
the payload-building logic is unit-testable in isolation; everything that
touches `hass` lives on `CloudFlowReporter`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from .const import (
    CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY,
    CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY,
    CONF_CLOUD_FLOW_GRID_ENTITY,
    CONF_CLOUD_FLOW_INVERT_GRID,
    CONF_CLOUD_FLOW_LOAD_ENTITY,
    CONF_CLOUD_FLOW_SOLAR_ENTITY,
    DEFAULT_CLOUD_FLOW_INTERVAL,
    DOMAIN,
    POWERSYNC_FLOW_API_URL,
    TESLA_PROVIDER_POWERSYNC,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# 404 means the ha_flow_reporter beta flag isn't granted on this account yet
# (expected during rollout). Back off for a while instead of hammering the
# endpoint every push interval.
_FLAG_NOT_ENABLED_BACKOFF_S = 30 * 60
# Network/5xx (and other unexpected-status) errors are logged at most this
# often so a prolonged cloud outage doesn't spam the HA log every push
# interval; the push loop itself keeps retrying at the normal interval.
_ERROR_LOG_INTERVAL_S = 60 * 60
_REQUEST_TIMEOUT_S = 15

_WATT_UNITS = {"w", "watt", "watts"}
_KW_UNITS = {"kw", "kilowatt", "kilowatts"}
_UNAVAILABLE_STATE_VALUES = {None, "", "unknown", "unavailable"}


def _state_value_kw(state: Any) -> float | None:
    """Convert a power sensor state to kW.

    Returns None when the state is missing/unavailable/unparseable, or its
    `unit_of_measurement` isn't W or kW — callers skip the field in that case.
    """
    if state is None:
        return None
    raw_state = getattr(state, "state", None)
    if raw_state in _UNAVAILABLE_STATE_VALUES:
        return None
    try:
        value = float(raw_state)
    except (TypeError, ValueError):
        return None

    attributes = getattr(state, "attributes", None) or {}
    unit = str(attributes.get("unit_of_measurement", "")).strip().lower()
    if unit in _KW_UNITS:
        return value
    if unit in _WATT_UNITS:
        return value / 1000.0
    return None


def _state_value_soc_fraction(state: Any) -> float | None:
    """Convert a battery SoC state (0-100 %) to a 0-1 fraction."""
    if state is None:
        return None
    raw_state = getattr(state, "state", None)
    if raw_state in _UNAVAILABLE_STATE_VALUES:
        return None
    try:
        value = float(raw_state)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, value / 100.0))


def _timestamp_ms(state: Any) -> int:
    """Return the grid state's last_updated in ms epoch, else now."""
    last_updated = getattr(state, "last_updated", None)
    if last_updated is not None:
        try:
            return int(last_updated.timestamp() * 1000)
        except (AttributeError, TypeError, ValueError, OSError):
            pass
    return int(time.time() * 1000)


def _collect_entity_ids(options: dict[str, Any]) -> list[str]:
    """Return the configured entity_ids worth reading from hass.states."""
    keys = (
        CONF_CLOUD_FLOW_GRID_ENTITY,
        CONF_CLOUD_FLOW_SOLAR_ENTITY,
        CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY,
        CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY,
        CONF_CLOUD_FLOW_LOAD_ENTITY,
    )
    return [options[key] for key in keys if options.get(key)]


def build_payload(
    states: dict[str, Any],
    options: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the ChargeHQ-shape /v1/flow payload from configured entities.

    `states` maps configured entity_id -> a duck-typed object exposing
    `.state`, `.attributes`, and `.last_updated` (matching
    `homeassistant.core.State`); an unknown/missing entity_id may simply be
    absent from the dict or mapped to None.

    Returns None when the grid entity is missing, unavailable, or reports an
    unrecognized unit — callers must skip the push entirely in that case,
    since `net_import_kw` is the one field the endpoint always needs.
    """
    grid_entity_id = options.get(CONF_CLOUD_FLOW_GRID_ENTITY)
    grid_state = states.get(grid_entity_id) if grid_entity_id else None
    grid_kw = _state_value_kw(grid_state)
    if grid_kw is None:
        return None

    if options.get(CONF_CLOUD_FLOW_INVERT_GRID, False):
        grid_kw = -grid_kw

    payload: dict[str, Any] = {
        "net_import_kw": grid_kw,
        "tsms": _timestamp_ms(grid_state),
        "source_id": "default",
    }

    solar_entity_id = options.get(CONF_CLOUD_FLOW_SOLAR_ENTITY)
    solar_kw = _state_value_kw(states.get(solar_entity_id)) if solar_entity_id else None
    if solar_kw is not None:
        payload["production_kw"] = solar_kw

    load_entity_id = options.get(CONF_CLOUD_FLOW_LOAD_ENTITY)
    load_kw = _state_value_kw(states.get(load_entity_id)) if load_entity_id else None
    if load_kw is not None:
        payload["consumption_kw"] = load_kw

    battery_power_entity_id = options.get(CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY)
    battery_kw = (
        _state_value_kw(states.get(battery_power_entity_id))
        if battery_power_entity_id
        else None
    )
    if battery_kw is not None:
        payload["battery_discharge_kw"] = battery_kw

    battery_soc_entity_id = options.get(CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY)
    battery_soc = (
        _state_value_soc_fraction(states.get(battery_soc_entity_id))
        if battery_soc_entity_id
        else None
    )
    if battery_soc is not None:
        payload["battery_soc"] = battery_soc

    return payload


class CloudFlowReporter:
    """Owns the background push loop for the PowerSync Cloud flow reporter."""

    def __init__(
        self,
        hass: "HomeAssistant",
        entry: "ConfigEntry",
        interval: int = DEFAULT_CLOUD_FLOW_INTERVAL,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._last_error_log_monotonic: float | None = None

    def start(self) -> None:
        """Start the background push loop."""
        if self._task is not None and not self._task.done():
            _LOGGER.warning("PowerSync cloud flow reporter is already running")
            return
        self._task = self._hass.async_create_background_task(
            self._run(),
            f"{DOMAIN}_cloud_flow_reporter_{self._entry.entry_id}",
        )

    async def stop(self) -> None:
        """Cancel the background push loop and wait for it to finish."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug(
                "PowerSync cloud flow reporter task raised while stopping: %s", err
            )

    def _entry_option(self, key: str, default: Any = None) -> Any:
        return self._entry.options.get(key, self._entry.data.get(key, default))

    def _options(self) -> dict[str, Any]:
        return {
            CONF_CLOUD_FLOW_GRID_ENTITY: self._entry_option(CONF_CLOUD_FLOW_GRID_ENTITY),
            CONF_CLOUD_FLOW_SOLAR_ENTITY: self._entry_option(CONF_CLOUD_FLOW_SOLAR_ENTITY),
            CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY: self._entry_option(
                CONF_CLOUD_FLOW_BATTERY_POWER_ENTITY
            ),
            CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY: self._entry_option(
                CONF_CLOUD_FLOW_BATTERY_SOC_ENTITY
            ),
            CONF_CLOUD_FLOW_LOAD_ENTITY: self._entry_option(CONF_CLOUD_FLOW_LOAD_ENTITY),
            CONF_CLOUD_FLOW_INVERT_GRID: self._entry_option(
                CONF_CLOUD_FLOW_INVERT_GRID, False
            ),
        }

    async def _run(self) -> None:
        _LOGGER.info(
            "PowerSync cloud flow reporter started (interval=%ds)", self._interval
        )
        try:
            while True:
                try:
                    delay = await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # pragma: no cover - defensive
                    self._log_error_once_per_hour(f"unexpected error: {err}")
                    delay = self._interval
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            _LOGGER.debug("PowerSync cloud flow reporter stopped")
            raise

    async def _tick(self) -> float:
        """Build and push one payload. Returns the delay before the next tick."""
        options = self._options()
        entity_ids = _collect_entity_ids(options)
        states = {
            entity_id: self._hass.states.get(entity_id) for entity_id in entity_ids
        }
        payload = build_payload(states, options)
        if payload is None:
            _LOGGER.debug(
                "PowerSync cloud flow reporter: grid entity unavailable, skipping push"
            )
            return self._interval

        # Lazy import: avoids a circular import at module load time (this
        # module is imported from __init__.py, which defines
        # get_tesla_api_token). By the time a tick runs, the power_sync
        # package has finished importing.
        from . import get_tesla_api_token

        token, provider = get_tesla_api_token(self._hass, self._entry)
        if provider != TESLA_PROVIDER_POWERSYNC or not token:
            self._log_error_once_per_hour(
                "no PowerSync (psync_) token available -- sign in with Tesla "
                "via PowerSync to use the cloud flow reporter"
            )
            return self._interval

        return await self._push(payload, token)

    async def _push(self, payload: dict[str, Any], token: str) -> float:
        """POST one payload to PowerSync Cloud. Returns the next-tick delay."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                POWERSYNC_FLOW_API_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT_S),
            ) as response:
                if response.status == 200:
                    self._last_error_log_monotonic = None
                    return self._interval

                if response.status == 429:
                    retry_after_ms = 0
                    try:
                        body = await response.json(content_type=None)
                        if isinstance(body, dict):
                            retry_after_ms = int(body.get("retry_after_ms") or 0)
                    except (aiohttp.ContentTypeError, ValueError, TypeError):
                        pass
                    delay = max(retry_after_ms / 1000.0, self._interval)
                    _LOGGER.debug(
                        "PowerSync cloud flow reporter: rate limited, backing off %.1fs",
                        delay,
                    )
                    return delay

                if response.status == 404:
                    _LOGGER.info(
                        "PowerSync cloud flow reporter: HA flow-reporter beta "
                        "flag is not enabled for this account yet -- backing "
                        "off %d minutes",
                        _FLAG_NOT_ENABLED_BACKOFF_S // 60,
                    )
                    return _FLAG_NOT_ENABLED_BACKOFF_S

                self._log_error_once_per_hour(
                    f"push rejected with HTTP {response.status}"
                )
                return self._interval
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            self._log_error_once_per_hour(f"network error: {err}")
            return self._interval

    def _log_error_once_per_hour(self, message: str) -> None:
        """Rate-limit WARNING logs so a persistent error doesn't spam the log."""
        now = time.monotonic()
        if (
            self._last_error_log_monotonic is not None
            and now - self._last_error_log_monotonic < _ERROR_LOG_INTERVAL_S
        ):
            return
        self._last_error_log_monotonic = now
        _LOGGER.warning("PowerSync cloud flow reporter: %s", message)
