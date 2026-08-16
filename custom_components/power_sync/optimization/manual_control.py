"""Project an active user-owned battery control into optimizer slots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any


_MODE_BY_CONTROL = {
    "charge": "charge",
    "discharge": "export",
    "export": "export",
    "hold_soc": "idle",
    "self_consumption": "self_use",
}


def _utc_datetime(value: Any) -> datetime | None:
    """Return an aware UTC datetime for a stored force timestamp."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _positive_power_w(value: Any, fallback: float) -> float:
    """Return a finite positive command power capped to the configured limit."""
    try:
        power_w = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        power_w = 0.0
    if not math.isfinite(power_w) or power_w <= 0:
        power_w = max(0.0, float(fallback or 0.0))
    configured = max(0.0, float(fallback or 0.0))
    if configured > 0:
        power_w = min(power_w, configured)
    return max(0.0, power_w)


@dataclass(frozen=True)
class ManualControlProjection:
    """Fixed optimizer inputs and public metadata for a manual control window."""

    control_type: str
    expires_at: datetime
    mode_slots: list[str | None]
    required_charge_kw: list[float]
    required_discharge_kw: list[float]
    active_slots: list[bool]

    @property
    def slot_count(self) -> int:
        return sum(self.active_slots)

    def optimizer_payload(self) -> dict[str, Any]:
        """Return the fixed-slot payload consumed by BatteryOptimizer."""
        return {
            "control_type": self.control_type,
            "mode_slots": list(self.mode_slots),
            "required_charge_kw": list(self.required_charge_kw),
            "required_discharge_kw": list(self.required_discharge_kw),
        }

    def status_payload(self) -> dict[str, Any]:
        """Return user-visible projection metadata without claiming execution."""
        return {
            "active": True,
            "control_type": self.control_type,
            "control_source": "manual",
            "projection": "planned",
            "expires_at": self.expires_at.isoformat(),
            "projected_slots": self.slot_count,
        }


def build_manual_control_projection(
    force_state: dict[str, Any] | None,
    timestamps: list[datetime],
    *,
    current_soc: float,
    capacity_wh: float,
    max_charge_w: float,
    max_discharge_w: float,
    hardware_reserve: float,
    efficiency: float,
    interval_minutes: int,
) -> ManualControlProjection | None:
    """Translate an active external force state into fixed LP slot inputs.

    Charge/discharge requests are capped to the energy physically available
    above the hardware floor or below full SOC. This keeps a long manual timer
    feasible after the battery reaches its natural limit while still letting
    the later horizon optimize from the projected SOC trajectory.
    """
    state = force_state or {}
    if not state.get("active") or state.get("source") == "optimizer":
        return None

    control_type = str(state.get("type") or "").strip().lower()
    mode = _MODE_BY_CONTROL.get(control_type)
    expires_at = _utc_datetime(state.get("expires_at"))
    if mode is None or expires_at is None or not timestamps:
        return None

    active_slots = []
    for timestamp in timestamps:
        slot_at = _utc_datetime(timestamp)
        active_slots.append(bool(slot_at is not None and slot_at < expires_at))
    if not any(active_slots):
        return None

    slot_hours = max(1, int(interval_minutes)) / 60.0
    capacity_kwh = max(0.0, float(capacity_wh or 0.0)) / 1000.0
    efficiency = max(1e-6, min(1.0, float(efficiency or 1.0)))
    projected_soc = max(0.0, min(1.0, float(current_soc or 0.0)))
    reserve = max(0.0, min(1.0, float(hardware_reserve or 0.0)))
    charge_kw = _positive_power_w(state.get("power_w"), max_charge_w) / 1000.0
    discharge_kw = (
        _positive_power_w(state.get("power_w"), max_discharge_w) / 1000.0
    )

    mode_slots: list[str | None] = []
    required_charge_kw: list[float] = []
    required_discharge_kw: list[float] = []
    for active in active_slots:
        if not active:
            mode_slots.append(None)
            required_charge_kw.append(0.0)
            required_discharge_kw.append(0.0)
            continue

        mode_slots.append(mode)
        slot_charge_kw = 0.0
        slot_discharge_kw = 0.0
        if control_type == "charge" and capacity_kwh > 0 and charge_kw > 0:
            input_headroom_kwh = max(
                0.0,
                (1.0 - projected_soc) * capacity_kwh / efficiency,
            )
            slot_charge_kw = min(charge_kw, input_headroom_kwh / slot_hours)
            projected_soc = min(
                1.0,
                projected_soc
                + slot_charge_kw * efficiency * slot_hours / capacity_kwh,
            )
        elif (
            control_type in {"discharge", "export"}
            and capacity_kwh > 0
            and discharge_kw > 0
        ):
            output_available_kwh = max(
                0.0,
                (projected_soc - reserve) * capacity_kwh * efficiency,
            )
            slot_discharge_kw = min(
                discharge_kw,
                output_available_kwh / slot_hours,
            )
            projected_soc = max(
                reserve,
                projected_soc
                - slot_discharge_kw * slot_hours / efficiency / capacity_kwh,
            )
        required_charge_kw.append(slot_charge_kw)
        required_discharge_kw.append(slot_discharge_kw)

    return ManualControlProjection(
        control_type=control_type,
        expires_at=expires_at,
        mode_slots=mode_slots,
        required_charge_kw=required_charge_kw,
        required_discharge_kw=required_discharge_kw,
        active_slots=active_slots,
    )
