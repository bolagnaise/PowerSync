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
    action: str  # Includes internal "solar_export" alongside legacy actions.
    power_w: float
    soc: float | None = None
    battery_charge_w: float = 0.0
    battery_discharge_w: float = 0.0
    reason: str | None = None
    # Grid-side EV charging power the optimizer planned for this slot. Zero
    # when no EV demand was modeled; the EV controller follows it so the car
    # and the battery share one import envelope.
    ev_charge_w: float = 0.0
    control_source: str | None = None
    control_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for API response."""
        legacy_action = (
            "self_consumption" if self.action == "solar_export" else self.action
        )
        payload = {
            "timestamp": self.timestamp.isoformat(),
            "action": legacy_action,
            "power_w": self.power_w,
            "soc": self.soc,
        }
        if self.action == "solar_export":
            payload["action_detail"] = "solar_export"
            payload["action_reason"] = self.reason or "profit_max_solar_export"
        elif self.reason:
            payload["action_reason"] = self.reason
        if self.ev_charge_w > 0:
            payload["ev_charge_w"] = round(self.ev_charge_w, 1)
        if self.control_source:
            payload["control_source"] = self.control_source
        if self.control_action:
            payload["control_action"] = self.control_action
        return payload


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
        """Get total battery charge power schedule (positive = charging)."""
        return [a.battery_charge_w for a in self.actions]

    @property
    def discharge_w(self) -> list[float]:
        """Get total battery discharge power schedule (positive = discharging)."""
        return [a.battery_discharge_w for a in self.actions]

    @property
    def ev_charging_w(self) -> list[float]:
        """Get the EV charging load modeled in each schedule slot."""
        return [max(0.0, float(a.ev_charge_w or 0.0)) for a in self.actions]

    @property
    def battery_consume_w(self) -> list[float]:
        """Get battery-to-home consumption power schedule."""
        values: list[float] = []
        for action in self.actions:
            if action.action in ("self_consumption", "consume", "off_grid"):
                values.append(action.battery_discharge_w)
                continue
            if action.action in ("export", "discharge"):
                export_w = max(0.0, min(action.power_w, action.battery_discharge_w))
                values.append(max(0.0, action.battery_discharge_w - export_w))
                continue
            values.append(0.0)
        return values

    @property
    def battery_export_w(self) -> list[float]:
        """Get battery-to-grid export power schedule."""
        return [
            max(0.0, min(a.power_w, a.battery_discharge_w))
            if a.action in ("export", "discharge")
            else 0.0
            for a in self.actions
        ]

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
            # Keep EV load inside the canonical schedule contract.  Adding it
            # later as a chart-only overlay leaves downstream flow rebuilds
            # with a house-only schedule and recreates impossible energy
            # balances even though the LP modeled the car correctly.
            "ev_charging_w": self.ev_charging_w,
            "battery_consume_w": self.battery_consume_w,
            "battery_export_w": self.battery_export_w,
            "soc": self.soc,
            "control_source": [a.control_source for a in self.actions],
            "control_action": [a.control_action for a in self.actions],
            "action_reason": [a.reason for a in self.actions],
            "grid_import_w": [],
            "grid_export_w": [],
        }
