"""Track externally-started Tesla charging sessions from observed telemetry."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

ACTIVE_POWER_THRESHOLD_KW = 0.05
OBSERVED_SESSION_MODE = "observed"
OBSERVED_SESSION_MODES = {OBSERVED_SESSION_MODE, "external"}


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _vehicle_id(vehicle: Mapping[str, Any]) -> str:
    vehicle_id = (
        vehicle.get("vehicle_id")
        or vehicle.get("vin")
        or vehicle.get("id")
        or vehicle.get("charger_id")
        or _normal_key(vehicle.get("vehicle_name") or vehicle.get("name"))
    )
    return str(vehicle_id or "").strip()


class ObservedTeslaSessionTracker:
    """Create charge-history sessions when Tesla charging starts outside PowerSync."""

    def __init__(
        self,
        hass,
        entry,
        session_manager,
        vehicle_status_fn: Callable[[Any, Any], list[dict[str, Any]]],
        idle_polls_to_end: int = 2,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._session_manager = session_manager
        self._vehicle_status_fn = vehicle_status_fn
        self._idle_polls_to_end = idle_polls_to_end
        self._idle_counts: dict[str, int] = {}

    def _entry_data(self) -> dict:
        return self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})

    def _site_snapshot(self) -> dict[str, float]:
        coordinator = self._entry_data().get("tesla_coordinator")
        data = getattr(coordinator, "data", None) or {}
        return {
            "grid_power_kw": _float_value(data.get("grid_power"), 0.0),
            "solar_power_kw": _float_value(data.get("solar_power"), 0.0),
        }

    def _has_fresh_tesla_data(self) -> bool:
        coordinator = self._entry_data().get("tesla_coordinator")
        return bool(getattr(coordinator, "data", None))

    def _is_mostly_solar(self, power_kw: float) -> bool:
        site = self._site_snapshot()
        if power_kw <= ACTIVE_POWER_THRESHOLD_KW:
            return False
        return site["solar_power_kw"] > 0 and site["grid_power_kw"] <= 0.1

    @staticmethod
    def _active_observed_session(manager, vehicle_id: str):
        session = manager.active_sessions.get(vehicle_id)
        if session and getattr(session, "mode", None) in OBSERVED_SESSION_MODES:
            return session
        return None

    @staticmethod
    def _has_other_active_session(manager, vehicle_id: str) -> bool:
        session = manager.active_sessions.get(vehicle_id)
        return bool(session and getattr(session, "mode", None) not in OBSERVED_SESSION_MODES)

    async def _mark_idle(self, vehicle_id: str, end_soc: int | None = None) -> None:
        if not self._active_observed_session(self._session_manager, vehicle_id):
            self._idle_counts.pop(vehicle_id, None)
            return

        idle_count = self._idle_counts.get(vehicle_id, 0) + 1
        self._idle_counts[vehicle_id] = idle_count
        if idle_count < self._idle_polls_to_end:
            return

        await self._session_manager.end_session(
            vehicle_id,
            reason="observed_charge_stopped",
            end_soc=end_soc,
        )
        self._idle_counts.pop(vehicle_id, None)
        _LOGGER.info("Observed Tesla charging session ended for %s", vehicle_id)

    async def poll(self, _now=None) -> None:
        """Poll live Tesla telemetry and update observed charging sessions."""
        if not self._has_fresh_tesla_data():
            return

        vehicles = self._vehicle_status_fn(self._hass, self._entry)
        seen_vehicle_ids: set[str] = set()

        for vehicle in vehicles:
            vehicle_id = _vehicle_id(vehicle)
            if not vehicle_id:
                continue
            seen_vehicle_ids.add(vehicle_id)

            power_kw = _float_value(
                vehicle.get("ev_power_kw", vehicle.get("current_power_kw")),
                0.0,
            )
            is_charging = (
                bool(vehicle.get("is_charging"))
                or power_kw > ACTIVE_POWER_THRESHOLD_KW
            )
            soc = _optional_int(vehicle.get("ev_soc", vehicle.get("current_soc")))

            if not is_charging:
                await self._mark_idle(vehicle_id, end_soc=soc)
                continue

            self._idle_counts[vehicle_id] = 0

            if self._has_other_active_session(self._session_manager, vehicle_id):
                continue

            if not self._active_observed_session(self._session_manager, vehicle_id):
                await self._session_manager.start_session(
                    vehicle_id=vehicle_id,
                    mode=OBSERVED_SESSION_MODE,
                    start_soc=soc,
                )
                _LOGGER.info("Observed Tesla charging session started for %s", vehicle_id)

            await self._session_manager.update_session(
                vehicle_id=vehicle_id,
                power_kw=power_kw,
                amps=_optional_int(vehicle.get("current_amps")) or 0,
                is_solar=self._is_mostly_solar(power_kw),
                battery_soc=soc,
            )

        for vehicle_id, session in list(self._session_manager.active_sessions.items()):
            if getattr(session, "mode", None) not in OBSERVED_SESSION_MODES:
                continue
            if vehicle_id not in seen_vehicle_ids:
                await self._mark_idle(vehicle_id)
