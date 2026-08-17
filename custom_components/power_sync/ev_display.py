"""Canonical EV display snapshots shared by HA and mobile dashboards."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any


ACTIVE_POWER_THRESHOLD_KW = 0.05


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_charging(loadpoint: dict[str, Any]) -> bool:
    if "actual_charging" in loadpoint:
        return bool(loadpoint.get("actual_charging"))
    if "is_charging" in loadpoint:
        return bool(loadpoint.get("is_charging"))
    return bool(
        loadpoint.get("status") == "charging"
        or _float_value(loadpoint.get("current_power_kw"))
        > ACTIVE_POWER_THRESHOLD_KW
    )


def active_display_loadpoint(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """Return the single loadpoint whose context should label HA EV sensors."""
    loadpoints = list(snapshot.get("loadpoints") or [])
    if not loadpoints:
        return None
    return (
        next((item for item in loadpoints if _is_charging(item)), None)
        or next((item for item in loadpoints if item.get("connected")), None)
        or loadpoints[0]
    )


def display_snapshot_to_sensor_data(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project the canonical display snapshot into HA EV sensor fields."""
    loadpoints = list(snapshot.get("loadpoints") or [])
    site = snapshot.get("site") or {}
    active = active_display_loadpoint(snapshot)
    data: dict[str, Any] = {
        "ev_power_kw": _float_value(site.get("ev_power_kw")),
        "vehicle_count": len(loadpoints),
        "loadpoint_count": len(loadpoints),
        "observation_quality": site.get("observation_quality"),
    }
    if active is None:
        return data

    data.update(
        {
            "vehicle_id": active.get("vehicle_id") or active.get("loadpoint_id"),
            "vehicle_name": active.get("vehicle_name") or "EV",
            "ev_soc": active.get("soc"),
            "is_connected": bool(active.get("connected")),
            "is_charging": _is_charging(active),
            "is_discharging": _float_value(active.get("current_power_kw")) < -0.05,
        }
    )
    if active.get("site_presence") in {"home", "away"}:
        data["site_presence"] = active.get("site_presence")
    return data


def display_snapshot_to_widgets(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the canonical display snapshot into the mobile widget contract."""
    site = snapshot.get("site") or {}
    surplus_kw = _float_value(site.get("surplus_kw"))
    widgets = []
    for loadpoint in snapshot.get("loadpoints") or []:
        power_kw = _float_value(loadpoint.get("current_power_kw"))
        widgets.append(
            {
                "vehicle_name": loadpoint.get("vehicle_name") or "EV",
                "vehicle_id": (
                    loadpoint.get("vehicle_id") or loadpoint.get("loadpoint_id")
                ),
                "charger_type": loadpoint.get("charger_type"),
                "is_charging": _is_charging(loadpoint),
                "is_connected": bool(loadpoint.get("connected")),
                "current_soc": loadpoint.get("soc") or 0,
                "target_soc": loadpoint.get("target_soc") or 80,
                "current_power_kw": round(power_kw, 2),
                "source": loadpoint.get("source") or (
                    "grid" if power_kw > ACTIVE_POWER_THRESHOLD_KW else "idle"
                ),
                "eta_minutes": loadpoint.get("duration_minutes"),
                "surplus_kw": round(surplus_kw, 2),
            }
        )
    return widgets


class EVDisplayCoordinator:
    """Refresh and publish one canonical EV snapshot to every display surface."""

    def __init__(
        self,
        async_loader: Callable[[], Awaitable[dict[str, Any]]],
        *,
        minimum_refresh_interval: timedelta = timedelta(seconds=1),
    ) -> None:
        self._async_loader = async_loader
        self._minimum_refresh_interval = minimum_refresh_interval
        self._snapshot: dict[str, Any] | None = None
        self._updated_at: datetime | None = None
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._inflight_refresh: asyncio.Task[dict[str, Any]] | None = None

    @property
    def snapshot(self) -> dict[str, Any] | None:
        """Return a defensive copy of the latest canonical snapshot."""
        return copy.deepcopy(self._snapshot)

    def async_add_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> Callable[[], None]:
        """Subscribe to canonical snapshot changes."""
        self._listeners.append(listener)

        def remove_listener() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove_listener

    def publish(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Publish a snapshot built by the canonical loadpoint status path."""
        normalized = copy.deepcopy(snapshot)
        changed = normalized != self._snapshot
        self._snapshot = normalized
        self._updated_at = datetime.now(timezone.utc)
        if changed:
            for listener in tuple(self._listeners):
                listener(copy.deepcopy(normalized))
        return copy.deepcopy(normalized)

    async def async_refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Return one recent snapshot, refreshing it through the shared loader."""
        task = self._inflight_refresh
        if task is not None and not task.done():
            return copy.deepcopy(await asyncio.shield(task))

        now = datetime.now(timezone.utc)
        if (
            not force
            and self._snapshot is not None
            and self._updated_at is not None
            and now - self._updated_at < self._minimum_refresh_interval
        ):
            return copy.deepcopy(self._snapshot)

        async def load_and_publish() -> dict[str, Any]:
            return self.publish(await self._async_loader())

        task = asyncio.create_task(load_and_publish())
        self._inflight_refresh = task
        try:
            return copy.deepcopy(await asyncio.shield(task))
        finally:
            if self._inflight_refresh is task and task.done():
                self._inflight_refresh = None

    async def async_request_refresh(self) -> dict[str, Any]:
        """Coalesce simultaneous telemetry-triggered refresh requests."""
        return await self.async_refresh(force=True)
