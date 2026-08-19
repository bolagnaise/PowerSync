"""Pure per-phase current allocation for PowerSync-owned EV charging."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Iterable, Mapping


PHASE_LOAD_MANAGEMENT_SCHEMA_VERSION = 1
PHASE_CURRENT_FRESHNESS_SECONDS = 90
DEFAULT_PHASE_CURRENT_SAFETY_MARGIN_AMPS = 2.0
PHASES = ("l1", "l2", "l3")

HOME_POWER_DEFAULTS: dict[str, Any] = {
    "phase_type": "single",
    "max_charge_speed_enabled": False,
    "max_amps_per_phase": 32,
    "max_grid_import_amps": 0,
    "default_voltage": 240,
    "phase_load_management_enabled": False,
    "phase_current_entity_l1": "",
    "phase_current_entity_l2": "",
    "phase_current_entity_l3": "",
    "phase_current_safety_margin_amps": DEFAULT_PHASE_CURRENT_SAFETY_MARGIN_AMPS,
}

_ENTITY_ID_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def normalize_home_power_settings(
    settings: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a complete, backwards-compatible Home Power settings object."""
    normalized = dict(HOME_POWER_DEFAULTS)
    if isinstance(settings, Mapping):
        normalized.update(settings)
    normalized["phase_load_management_enabled"] = bool(
        normalized.get("phase_load_management_enabled", False)
    )
    for phase in PHASES:
        key = f"phase_current_entity_{phase}"
        normalized[key] = str(normalized.get(key) or "").strip().lower()
    try:
        normalized["phase_current_safety_margin_amps"] = float(
            normalized.get(
                "phase_current_safety_margin_amps",
                DEFAULT_PHASE_CURRENT_SAFETY_MARGIN_AMPS,
            )
        )
    except (TypeError, ValueError):
        normalized["phase_current_safety_margin_amps"] = (
            DEFAULT_PHASE_CURRENT_SAFETY_MARGIN_AMPS
        )
    return normalized


def required_phases(settings: Mapping[str, Any]) -> tuple[str, ...]:
    """Return phases that must have current telemetry for these settings."""
    return PHASES if settings.get("phase_type") == "three" else ("l1",)


def validate_home_power_settings(settings: Mapping[str, Any]) -> str | None:
    """Return a user-facing validation error, or ``None`` when valid."""
    normalized = normalize_home_power_settings(settings)
    if normalized.get("phase_type") not in ("single", "three"):
        return "phase_type must be 'single' or 'three'"
    if not normalized["phase_load_management_enabled"]:
        return None

    try:
        breaker_amps = float(normalized.get("max_grid_import_amps") or 0)
        margin_amps = float(normalized["phase_current_safety_margin_amps"])
    except (TypeError, ValueError):
        return "Breaker size and safety margin must be numeric"
    if not math.isfinite(breaker_amps) or breaker_amps <= 0:
        return "Main breaker size must be greater than 0 A"
    if not math.isfinite(margin_amps) or margin_amps < 0:
        return "Safety margin must be 0 A or greater"
    if margin_amps >= breaker_amps:
        return "Safety margin must be lower than the main breaker size"

    entity_ids: list[str] = []
    for phase in required_phases(normalized):
        entity_id = normalized[f"phase_current_entity_{phase}"]
        if not entity_id:
            return f"{phase.upper()} current entity is required"
        if not _ENTITY_ID_RE.fullmatch(entity_id):
            return f"{phase.upper()} current entity must be a valid Home Assistant entity ID"
        entity_ids.append(entity_id)
    if len(set(entity_ids)) != len(entity_ids):
        return "Each phase must use a different current entity"
    return None


@dataclass(frozen=True)
class PhaseSample:
    """One validated current reading in amperes."""

    phase: str
    amps: float
    age_seconds: float
    entity_id: str = ""


@dataclass(frozen=True)
class LoadpointRequest:
    """Requested and observed state for one PowerSync-owned loadpoint."""

    loadpoint_id: str
    requested_amps: float
    min_amps: int
    max_amps: int
    phases: frozenset[str]
    current_amps: float = 0.0
    observed_amps: float | None = None
    priority: int = 1


@dataclass(frozen=True)
class PhaseAllocation:
    """A deterministic site-wide allocation and its diagnostics."""

    allocations: dict[str, int]
    reasons: dict[str, str]
    phase_readings_amps: dict[str, float] = field(default_factory=dict)
    phase_budgets_amps: dict[str, float] = field(default_factory=dict)
    limiting_phase: str | None = None
    telemetry_valid: bool = True
    telemetry_reason: str | None = None


def allocate_phase_currents(
    *,
    samples: Mapping[str, PhaseSample] | None,
    breaker_amps: float,
    safety_margin_amps: float,
    loadpoints: Iterable[LoadpointRequest],
    telemetry_reason: str | None = None,
) -> PhaseAllocation:
    """Allocate a shared managed-current budget without exceeding any phase."""
    records = list(loadpoints)
    if telemetry_reason or not samples:
        reason = telemetry_reason or "phase telemetry unavailable"
        return PhaseAllocation(
            allocations={record.loadpoint_id: 0 for record in records},
            reasons={record.loadpoint_id: "telemetry_invalid" for record in records},
            telemetry_valid=False,
            telemetry_reason=reason,
        )

    configured_phases = frozenset(samples)
    trusted_owned = {phase: 0.0 for phase in configured_phases}
    for record in records:
        if record.observed_amps is None:
            continue
        try:
            observed = float(record.observed_amps)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(observed) or observed < 0:
            continue
        footprint = record.phases & configured_phases or configured_phases
        for phase in footprint:
            trusted_owned[phase] += observed

    phase_limit = max(0.0, float(breaker_amps) - float(safety_margin_amps))
    readings = {
        phase: max(0.0, abs(float(sample.amps)))
        for phase, sample in samples.items()
    }
    budgets = {
        phase: max(
            0.0,
            phase_limit - max(0.0, readings[phase] - trusted_owned[phase]),
        )
        for phase in configured_phases
    }
    original_budgets = dict(budgets)
    limiting_phase = min(budgets, key=lambda phase: (budgets[phase], phase))

    # Existing active sessions retain deterministic priority before new starts.
    ordered = sorted(
        records,
        key=lambda record: (
            0 if record.current_amps > 0 else 1,
            int(record.priority),
            record.loadpoint_id,
        ),
    )
    allocations: dict[str, int] = {}
    reasons: dict[str, str] = {}
    for record in ordered:
        footprint = record.phases & configured_phases or configured_phases
        requested = min(float(record.requested_amps), float(record.max_amps))
        requested = max(0.0, requested)
        phase_cap = min((budgets[phase] for phase in footprint), default=0.0)
        allocated = min(requested, phase_cap)
        allocated_int = int(math.floor(allocated + 1e-9))
        if allocated_int < max(1, int(record.min_amps)):
            allocated_int = 0
        allocations[record.loadpoint_id] = allocated_int
        if allocated_int <= 0 and requested > 0:
            reasons[record.loadpoint_id] = "below_minimum"
        elif allocated_int < int(math.floor(requested + 1e-9)):
            reasons[record.loadpoint_id] = "phase_limited"
        else:
            reasons[record.loadpoint_id] = "allocated"
        for phase in footprint:
            budgets[phase] = max(0.0, budgets[phase] - allocated_int)

    return PhaseAllocation(
        allocations=allocations,
        reasons=reasons,
        phase_readings_amps=readings,
        phase_budgets_amps=original_budgets,
        limiting_phase=limiting_phase,
    )
