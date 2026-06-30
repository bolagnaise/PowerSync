"""Shared EV dashboard charging policy helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


EV_POLICY_SOLAR_ONLY = "solar_only"
EV_POLICY_LIMITED_GRID_SOLAR = "limited_grid_solar"
EV_POLICY_FULL_GRID_SOLAR = "full_grid_solar"
VALID_EV_CHARGING_POLICIES = (
    EV_POLICY_SOLAR_ONLY,
    EV_POLICY_LIMITED_GRID_SOLAR,
    EV_POLICY_FULL_GRID_SOLAR,
)
EV_POLICY_DURATION_MIN = 1
EV_POLICY_DURATION_MAX = 1440


@dataclass(frozen=True)
class EVPolicyAction:
    """Resolved backend action for a dashboard EV charging policy."""

    action_type: str
    params: dict[str, Any]
    label: str


def validate_ev_policy_duration(value: Any) -> int:
    """Validate and normalize dashboard EV charge duration."""
    try:
        duration = int(value)
    except (TypeError, ValueError):
        raise ValueError("Invalid duration_minutes value") from None

    if not EV_POLICY_DURATION_MIN <= duration <= EV_POLICY_DURATION_MAX:
        raise ValueError("duration_minutes must be 1-1440")
    return duration


def validate_ev_charging_policy(value: Any) -> str:
    """Validate and normalize a dashboard EV charging source policy."""
    policy = str(value or "").strip()
    if policy not in VALID_EV_CHARGING_POLICIES:
        valid = ", ".join(VALID_EV_CHARGING_POLICIES)
        raise ValueError(f"policy must be one of: {valid}")
    return policy


def build_ev_policy_action(policy_value: Any, duration_value: Any) -> EVPolicyAction:
    """Map a dashboard source policy to the existing EV action layer."""
    policy = validate_ev_charging_policy(policy_value)
    duration_minutes = validate_ev_policy_duration(duration_value)
    common_params: dict[str, Any] = {
        "duration_minutes": duration_minutes,
        "quick_control": True,
        "source_mode": policy,
    }

    if policy == EV_POLICY_SOLAR_ONLY:
        return EVPolicyAction(
            action_type="start_ev_charging_dynamic",
            params={
                **common_params,
                "dynamic_mode": "solar_surplus",
                "owner_mode": "manual_solar_surplus",
                "notify_on_start": False,
            },
            label="Solar-only EV charging started",
        )

    if policy == EV_POLICY_LIMITED_GRID_SOLAR:
        return EVPolicyAction(
            action_type="start_ev_charging_dynamic",
            params={
                **common_params,
                "dynamic_mode": "battery_target",
                "owner_mode": "manual_limited_grid_solar",
                "notify_on_start": False,
            },
            label="Limited grid + solar EV charging started",
        )

    return EVPolicyAction(
        action_type="start_ev_charging",
        params={
            **common_params,
            "source_mode": "grid_allowed",
            "source_policy": EV_POLICY_FULL_GRID_SOLAR,
        },
        label="Full grid + solar EV charging started",
    )
