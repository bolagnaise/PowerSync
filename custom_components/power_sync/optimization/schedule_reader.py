"""
Schedule data models for PowerSync optimization.

Provides the ScheduleAction and OptimizationSchedule dataclasses used by
the built-in LP optimizer and the execution layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class ScheduleAction:
    """Single action in the optimization schedule."""
    timestamp: datetime
    action: str  # "idle", "charge", "discharge", "consume", "export", "self_consumption"
    power_w: float
    soc: float | None = None
    battery_charge_w: float = 0.0
    battery_discharge_w: float = 0.0

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
        """Get battery charge power schedule (positive = charging)."""
        return [a.battery_charge_w if a.action == "charge" else 0.0 for a in self.actions]

    @property
    def discharge_w(self) -> list[float]:
        """Get battery discharge power schedule (positive = discharging)."""
        return [a.battery_discharge_w if a.action in ("discharge", "export") else 0.0 for a in self.actions]

    @property
    def soc(self) -> list[float]:
        """Get SOC schedule (0-1 scale)."""
        return [a.soc if a.soc is not None else 0.5 for a in self.actions]

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


