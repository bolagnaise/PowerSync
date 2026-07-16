"""Monitoring-mode transition helpers."""

from __future__ import annotations

from typing import Any

from .const import DOMAIN, SERVICE_RESTORE_NORMAL

_HANDOFF_ACTIVE = "_monitoring_handoff_active"


async def async_prepare_monitoring_handoff(hass: Any, entry: Any) -> None:
    """Release PowerSync control before persisting monitoring mode.

    Call this only for a verified disabled-to-enabled transition. Ordinary
    saves and lifecycle reloads while monitoring is already active must remain
    write-free.
    """
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data[_HANDOFF_ACTIVE] = True
    coordinator = entry_data.get("optimization_coordinator")
    execute_lock = getattr(coordinator, "_execute_lock", None)
    lock_acquired = False

    if execute_lock is not None:
        await execute_lock.acquire()
        lock_acquired = True

    active_state_keys = ("force_charge_state", "force_discharge_state")
    active_before = {
        key
        for key in active_state_keys
        if isinstance(entry_data.get(key), dict)
        and entry_data[key].get("active", False)
    }

    try:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_NORMAL,
            {"source": "manual", "_force_restore": True},
            blocking=True,
        )

        still_active = {
            key
            for key in active_before
            if isinstance(entry_data.get(key), dict)
            and entry_data[key].get("active", False)
        }
        if still_active:
            raise RuntimeError(
                "restore normal left active control state: "
                + ", ".join(sorted(still_active))
            )

        if (
            coordinator
            and getattr(coordinator, "_pre_idle_backup_reserve", None) is not None
            and getattr(coordinator, "battery_controller", None)
        ):
            restored = await coordinator._restore_pre_idle_backup_reserve(
                coordinator.battery_controller,
                "monitoring enabled",
                bypass_monitoring=True,
            )
            if not restored:
                raise RuntimeError("pre-IDLE backup reserve restore failed")
    finally:
        if lock_acquired:
            execute_lock.release()


def finish_monitoring_handoff(hass: Any, entry: Any) -> None:
    """Clear the temporary write-block after monitoring persistence."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    if isinstance(entry_data, dict):
        entry_data.pop(_HANDOFF_ACTIVE, None)
