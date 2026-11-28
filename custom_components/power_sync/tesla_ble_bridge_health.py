"""Repairs notices for Tesla BLE bridges that need a firmware update.

PowerSync reads two things from each configured ESPHome Tesla BLE bridge:

* ``sensor.<prefix>_charge_current_request_max``: the car-reported maximum
  current for the connected charger, published by the PowerSync companion
  firmware from v2.12.1870. Without it PowerSync cannot tell the car's real
  limit from the bridge's build-time ``charging_amps_max``.
* ``number.<prefix>_charging_amps`` ``max``: fixed by ``charging_amps_max``
  when the bridge firmware is built. Home Assistant rejects any request above
  it, so a low value caps every BLE charging mode.

This module only reads entity state and registry data. It sends no vehicle or
bridge command.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

_LOGGER = logging.getLogger(__name__)

DOMAIN = "power_sync"
ISSUE_UPDATE = "tesla_ble_bridge_update"
ISSUE_CAPPED = "tesla_ble_bridge_capped"
GUIDE_URL = (
    "https://github.com/bolagnaise/PowerSync/blob/main/docs/tesla-ble.md"
    "#update-your-tesla-ble-bridge"
)


@dataclass(frozen=True)
class BridgeHealth:
    prefix: str
    present: bool
    reports_car_limit: bool
    amps_max: float | None
    car_limit: float | None

    @property
    def needs_update(self) -> bool:
        return self.present and not self.reports_car_limit

    @property
    def capped(self) -> bool:
        """The bridge's own maximum is below the car-reported limit."""
        return (
            self.present
            and self.amps_max is not None
            and self.car_limit is not None
            and self.amps_max + 0.5 < self.car_limit
        )


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def bridge_health(hass: Any, prefix: str, registry_ids: Iterable[str]) -> BridgeHealth:
    """Assess one bridge from current state and the entity registry ids."""
    ids = set(registry_ids)
    amps_entity = f"number.{prefix}_charging_amps"
    limit_entity = f"sensor.{prefix}_charge_current_request_max"
    present = amps_entity in ids or hass.states.get(amps_entity) is not None
    amps_state = hass.states.get(amps_entity)
    amps_max = _finite(getattr(amps_state, "attributes", {}).get("max")) if amps_state else None
    limit_state = hass.states.get(limit_entity)
    car_limit = _finite(getattr(limit_state, "state", None)) if limit_state else None
    return BridgeHealth(
        prefix=prefix,
        present=present,
        reports_car_limit=limit_entity in ids or limit_state is not None,
        amps_max=amps_max,
        car_limit=car_limit,
    )


def configured_prefixes(config: Mapping[str, Any]) -> list[str]:
    from .tesla_ble_mapping import configured_ble_prefixes

    provider = str(config.get("ev_provider") or "").strip().lower()
    if provider and provider not in {"tesla_ble", "both"}:
        return []
    return configured_ble_prefixes(config)


async def async_check_tesla_ble_bridges(hass: Any, entry: Any) -> list[BridgeHealth]:
    """Create or clear the bridge Repairs notices for one config entry."""
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir

    config = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    prefixes = configured_prefixes(config)
    registry = er.async_get(hass)
    registry_ids = list(registry.entities.keys()) if hasattr(registry.entities, "keys") else [
        e.entity_id for e in registry.entities.values()
    ]
    results: list[BridgeHealth] = []
    for prefix in prefixes:
        health = bridge_health(hass, prefix, registry_ids)
        results.append(health)
        update_id = f"{ISSUE_UPDATE}_{prefix}"
        capped_id = f"{ISSUE_CAPPED}_{prefix}"
        if health.needs_update:
            ir.async_create_issue(
                hass,
                DOMAIN,
                update_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_UPDATE,
                translation_placeholders={
                    "prefix": prefix,
                    "amps_max": (
                        f"{health.amps_max:.0f}" if health.amps_max is not None else "unknown"
                    ),
                    "guide": GUIDE_URL,
                },
                learn_more_url=GUIDE_URL,
            )
        elif health.present and health.reports_car_limit:
            # Only clear once the bridge proves it has the new firmware; a
            # missing bridge keeps any earlier notice.
            ir.async_delete_issue(hass, DOMAIN, update_id)
        if health.capped:
            ir.async_create_issue(
                hass,
                DOMAIN,
                capped_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_CAPPED,
                translation_placeholders={
                    "prefix": prefix,
                    "amps_max": f"{health.amps_max:.0f}",
                    "car_limit": f"{health.car_limit:.0f}",
                    "guide": GUIDE_URL,
                },
                learn_more_url=GUIDE_URL,
            )
        elif health.present and health.amps_max is not None and health.car_limit is not None:
            ir.async_delete_issue(hass, DOMAIN, capped_id)
    return results


FIRST_CHECK_DELAY_SECONDS = 120
CHECK_INTERVAL_SECONDS = 6 * 3600


def start(hass: Any, entry: Any) -> None:
    """Check after setup settles and every six hours until the entry unloads."""
    from datetime import timedelta

    from homeassistant.helpers.event import async_call_later, async_track_time_interval

    async def _run(_now=None) -> None:
        try:
            await async_check_tesla_ble_bridges(hass, entry)
        except Exception:
            # A diagnostics notice must never disturb charging control.
            _LOGGER.debug("Tesla BLE bridge health check failed", exc_info=True)

    entry.async_on_unload(async_call_later(hass, FIRST_CHECK_DELAY_SECONDS, _run))
    entry.async_on_unload(
        async_track_time_interval(hass, _run, timedelta(seconds=CHECK_INTERVAL_SECONDS))
    )
