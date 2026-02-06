"""
Schedule data models for PowerSync optimization.

Provides the ScheduleAction and OptimizationSchedule dataclasses used by
the built-in LP optimizer and the execution layer.

Previously this module also read from external optimizer sensors (HAEO).
That functionality has been removed — the built-in optimizer produces
schedules directly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScheduleAction:
    """Single action in the optimization schedule."""
    timestamp: datetime
    action: str  # "idle", "charge", "discharge", "consume", "export", "self_consumption"
    power_w: float
    soc: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "action": self.action,
            "power_w": self.power_w,
            "soc": self.soc,
        }


@dataclass
class OptimizationSchedule:
    """Complete optimization schedule."""
    actions: list[ScheduleAction]
    predicted_cost: float
    predicted_savings: float
    last_updated: datetime | None = None

    @property
    def timestamps(self) -> list[str]:
        """Get list of timestamps as ISO strings."""
        return [a.timestamp.isoformat() for a in self.actions]

    @property
    def charge_w(self) -> list[float]:
        """Get charge power schedule (positive = charging)."""
        return [
            a.power_w if a.action == "charge" else 0.0
            for a in self.actions
        ]

    @property
    def discharge_w(self) -> list[float]:
        """Get discharge power schedule (positive = discharging)."""
        return [
            a.power_w if a.action in ("discharge", "consume", "export") else 0.0
            for a in self.actions
        ]

    @property
    def soc(self) -> list[float]:
        """Get SOC schedule (0-1 scale)."""
        return [a.soc or 0.5 for a in self.actions]

    def to_executor_schedule(self) -> list[dict[str, Any]]:
        """Convert to executor-compatible format."""
        return [a.to_dict() for a in self.actions]

    def to_api_response(self) -> dict[str, Any]:
        """Convert to API response format for mobile app."""
        return {
            "timestamps": self.timestamps,
            "charge_w": self.charge_w,
            "discharge_w": self.discharge_w,
            "soc": self.soc,
            "grid_import_w": [],
            "grid_export_w": [],
        }


class ScheduleReader:
    """Legacy schedule reader (deprecated).

    Previously read optimization schedules from external HAEO sensors.
    The built-in LP optimizer now produces schedules directly.
    Kept as a stub for backward compatibility.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def get_schedule(self) -> OptimizationSchedule | None:
        """Read schedule (deprecated — returns None)."""
        return None

    async def get_current_action(self) -> ScheduleAction | None:
        """Get current action (deprecated — returns None)."""
        return None

    def is_available(self) -> bool:
        """Check if optimizer sensors are available (deprecated — returns False)."""
        return False
