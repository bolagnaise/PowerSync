"""Provider-neutral EV load attribution and Home Load normalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum, IntEnum
import math
from typing import Any, Iterable


class EvMeasurementKind(IntEnum):
    """Preference order for competing views of one physical loadpoint."""

    INFERRED = 0
    DERIVED = 1
    VEHICLE = 2
    INTEGRATED_CHARGER = 3
    LOADPOINT_METER = 4


class EvLoadQuality(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"


class HomeLoadBasis(str, Enum):
    INCLUDES_EV = "includes_ev"
    EXCLUDES_EV = "excludes_ev"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvLoadObservation:
    """One timestamped power view for a physical charging loadpoint."""

    physical_load_key: str
    source_key: str
    power_kw: float | None
    observed_at: datetime
    active: bool | None = None
    measurement_kind: EvMeasurementKind = EvMeasurementKind.VEHICLE
    supports_bidirectional_power: bool = False


@dataclass(frozen=True)
class ObservedEvLoadSnapshot:
    """One selected power reading per distinct physical loadpoint."""

    power_kw: float
    components: tuple[EvLoadObservation, ...]
    observed_at: datetime
    quality: EvLoadQuality
    unavailable_active_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class SitePowerSnapshot:
    """A raw site load and its single normalized non-EV Home Load result."""

    raw_home_load_kw: float | None
    raw_home_load_basis: HomeLoadBasis
    observed_ev_load_kw: float
    non_ev_home_load_kw: float | None
    timestamp: datetime
    normalization_quality: EvLoadQuality


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_power_kw(
    value: Any,
    unit: Any = "kW",
    *,
    supports_bidirectional_power: bool = False,
) -> float | None:
    """Normalize a measured charger value to finite signed kW."""
    try:
        power = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(power):
        return None
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit in ("w", "watt", "watts"):
        power /= 1000.0
    elif normalized_unit not in ("kw", "kilowatt", "kilowatts"):
        return None
    if power < 0 and not supports_bidirectional_power:
        return None
    return 0.0 if abs(power) < 0.001 else power


def meter_physical_load_key(
    *,
    charger_type: Any,
    entity_id: Any,
    entry_id: Any,
    native_id: Any = None,
    zaptec_id: Any = None,
    sigenergy_type: Any = None,
) -> str:
    """Return the canonical identity for an entity-backed charger meter."""
    charger = str(charger_type or "generic").strip().lower()
    if charger == "ocpp" and native_id is not None:
        return f"ocpp:{native_id}:1"
    if charger == "zaptec":
        return f"zaptec:{zaptec_id or 'standalone'}"
    if charger == "sigenergy":
        return f"sigenergy:{str(sigenergy_type or 'evac').lower()}"
    if charger == "solaredge":
        return f"solaredge:{entry_id}:internal"
    return f"generic:{str(entity_id or entry_id).strip()}"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def aggregate_ev_load(
    observations: Iterable[EvLoadObservation],
    *,
    at: datetime | None = None,
    max_age: timedelta = timedelta(seconds=90),
) -> ObservedEvLoadSnapshot:
    """Select one fresh reading per physical load and sum distinct loads."""
    target = _as_utc(at or utc_now())
    eligible: dict[str, list[EvLoadObservation]] = {}
    unavailable_active: set[str] = set()

    for observation in observations:
        physical_key = str(observation.physical_load_key or "").strip()
        source_key = str(observation.source_key or "").strip()
        if not physical_key or not source_key:
            continue
        observed_at = _as_utc(observation.observed_at)
        age = target - observed_at
        if age < timedelta(0) or age > max_age:
            if observation.active:
                unavailable_active.add(physical_key)
            continue
        power_kw = normalize_power_kw(
            observation.power_kw,
            "kW",
            supports_bidirectional_power=observation.supports_bidirectional_power,
        )
        if power_kw is None:
            if observation.active:
                unavailable_active.add(physical_key)
            continue
        eligible.setdefault(physical_key, []).append(
            EvLoadObservation(
                physical_load_key=physical_key,
                source_key=source_key,
                power_kw=power_kw,
                observed_at=observed_at,
                active=observation.active,
                measurement_kind=observation.measurement_kind,
                supports_bidirectional_power=observation.supports_bidirectional_power,
            )
        )

    selected: list[EvLoadObservation] = []
    for physical_key, candidates in eligible.items():
        choice = max(
            candidates,
            key=lambda item: (int(item.measurement_kind), _as_utc(item.observed_at)),
        )
        selected.append(choice)
        unavailable_active.discard(physical_key)

    selected.sort(key=lambda item: item.physical_load_key)
    quality = (
        EvLoadQuality.INCOMPLETE
        if unavailable_active
        else EvLoadQuality.COMPLETE
    )
    return ObservedEvLoadSnapshot(
        power_kw=sum(float(item.power_kw or 0.0) for item in selected),
        components=tuple(selected),
        observed_at=target,
        quality=quality,
        unavailable_active_keys=tuple(sorted(unavailable_active)),
    )


def reconcile_ev_load_snapshot(
    snapshot: Any,
    *,
    at: datetime | None = None,
    fallback_power_kw: Any = 0.0,
    fallback_by_physical_key: dict[str, Any] | None = None,
    fallback_observed_at: datetime | None = None,
    max_age: timedelta = timedelta(seconds=90),
) -> ObservedEvLoadSnapshot:
    """Fill missing physical loads from current backend-scoped direct meters.

    The aggregate snapshot remains authoritative for every loadpoint it can
    measure. A backend fallback may only replace the exact physical keys it
    owns and only when its source timestamp is not older; it must never make a
    distinct unmeasured charger look complete.
    """
    target = _as_utc(at or utc_now())
    fallback_keys: set[str] = set()
    candidate_fallbacks: dict[str, float] = {}
    for key, value in (fallback_by_physical_key or {}).items():
        physical_key = str(key or "").strip()
        power_kw = normalize_power_kw(value, "kW")
        if physical_key and power_kw is not None:
            fallback_keys.add(physical_key)
            candidate_fallbacks[physical_key] = power_kw

    try:
        direct_observed_at = (
            _as_utc(fallback_observed_at)
            if fallback_observed_at is not None
            else None
        )
    except (AttributeError, TypeError, ValueError):
        direct_observed_at = None
    direct_age = (
        target - direct_observed_at
        if direct_observed_at is not None
        else None
    )
    direct_is_current = bool(
        direct_age is not None and timedelta(0) <= direct_age <= max_age
    )
    normalized_fallbacks = candidate_fallbacks if direct_is_current else {}

    fallback_power = normalize_power_kw(fallback_power_kw, "kW") or 0.0
    if fallback_keys and not direct_is_current:
        fallback_power = 0.0
    if snapshot is None:
        if fallback_keys and not direct_is_current:
            return ObservedEvLoadSnapshot(
                power_kw=0.0,
                components=(),
                observed_at=target,
                quality=EvLoadQuality.INCOMPLETE,
                unavailable_active_keys=tuple(sorted(fallback_keys)),
            )
        return ObservedEvLoadSnapshot(
            power_kw=fallback_power,
            components=(),
            observed_at=target,
            quality=EvLoadQuality.COMPLETE,
        )

    components = tuple(getattr(snapshot, "components", ()) or ())
    unavailable_keys = {
        str(key)
        for key in (getattr(snapshot, "unavailable_active_keys", ()) or ())
        if key
    }
    observed_at = getattr(snapshot, "observed_at", None)
    try:
        age = target - _as_utc(observed_at)
    except (AttributeError, TypeError, ValueError):
        age = max_age + timedelta(seconds=1)

    def _fallback_observation(physical_key: str) -> EvLoadObservation:
        assert direct_observed_at is not None
        power_kw = normalized_fallbacks[physical_key]
        return EvLoadObservation(
            physical_load_key=physical_key,
            source_key="backend_direct_meter",
            power_kw=power_kw,
            observed_at=direct_observed_at,
            active=power_kw > 0.05,
            measurement_kind=EvMeasurementKind.LOADPOINT_METER,
        )

    if not timedelta(0) <= age <= max_age:
        stale_keys = {
            str(getattr(component, "physical_load_key", "") or "")
            for component in components
        }
        stale_keys.update(unavailable_keys)
        stale_keys.discard("")
        if (
            stale_keys
            and normalized_fallbacks
            and stale_keys <= normalized_fallbacks.keys()
        ):
            replacements = tuple(
                _fallback_observation(key) for key in sorted(stale_keys)
            )
            return ObservedEvLoadSnapshot(
                power_kw=sum(float(item.power_kw or 0.0) for item in replacements),
                components=replacements,
                observed_at=target,
                quality=EvLoadQuality.COMPLETE,
            )
        had_observed_loadpoints = bool(components or unavailable_keys)
        return ObservedEvLoadSnapshot(
            power_kw=fallback_power,
            components=components,
            observed_at=target,
            quality=(
                EvLoadQuality.INCOMPLETE
                if had_observed_loadpoints
                else EvLoadQuality.COMPLETE
            ),
            unavailable_active_keys=tuple(sorted(stale_keys or unavailable_keys)),
        )

    quality = getattr(snapshot, "quality", None)
    quality_value = getattr(quality, "value", quality)

    # A backend-direct meter is newer than the cached aggregate for the exact
    # physical load it owns.  This is especially important at charger edges:
    # the Wall Connector can report 0 W immediately while the independently
    # refreshed vehicle snapshot still contains the previous charging power (or
    # vice versa).  Preserve every unrelated component and only replace keys
    # with an explicit identity match.
    reconciled_components: list[EvLoadObservation] = []
    reconciled_power = float(getattr(snapshot, "power_kw", 0.0) or 0.0)
    replaced_keys: set[str] = set()
    for component in components:
        physical_key = str(
            getattr(component, "physical_load_key", "") or ""
        ).strip()
        if physical_key not in normalized_fallbacks:
            reconciled_components.append(component)
            continue

        try:
            component_observed_at = _as_utc(
                getattr(component, "observed_at", None)
            )
        except (AttributeError, TypeError, ValueError):
            # Without both source timestamps there is no safe basis for
            # replacing a current same-key observation.
            reconciled_components.append(component)
            continue
        if direct_observed_at < component_observed_at:
            reconciled_components.append(component)
            continue

        old_power = normalize_power_kw(
            getattr(component, "power_kw", None),
            "kW",
            supports_bidirectional_power=bool(
                getattr(component, "supports_bidirectional_power", False)
            ),
        ) or 0.0
        reconciled_power -= old_power
        if physical_key not in replaced_keys:
            replacement = _fallback_observation(physical_key)
            reconciled_components.append(replacement)
            reconciled_power += float(replacement.power_kw or 0.0)
            replaced_keys.add(physical_key)

    filled_unavailable_keys = unavailable_keys & normalized_fallbacks.keys()
    for physical_key in sorted(filled_unavailable_keys - replaced_keys):
        replacement = _fallback_observation(physical_key)
        reconciled_components.append(replacement)
        reconciled_power += float(replacement.power_kw or 0.0)

    if replaced_keys or filled_unavailable_keys:
        remaining_unavailable_keys = unavailable_keys - normalized_fallbacks.keys()
        original_complete = quality_value == EvLoadQuality.COMPLETE.value
        reconciled_complete = original_complete or (
            bool(unavailable_keys) and not remaining_unavailable_keys
        )
        return ObservedEvLoadSnapshot(
            power_kw=reconciled_power,
            components=tuple(reconciled_components),
            observed_at=target,
            quality=(
                EvLoadQuality.COMPLETE
                if reconciled_complete
                else EvLoadQuality.INCOMPLETE
            ),
            unavailable_active_keys=tuple(sorted(remaining_unavailable_keys)),
        )

    if quality_value == EvLoadQuality.COMPLETE.value:
        return ObservedEvLoadSnapshot(
            power_kw=float(getattr(snapshot, "power_kw", 0.0) or 0.0),
            components=components,
            observed_at=_as_utc(observed_at),
            quality=EvLoadQuality.COMPLETE,
        )
    return ObservedEvLoadSnapshot(
        power_kw=float(getattr(snapshot, "power_kw", 0.0) or 0.0),
        components=components,
        observed_at=_as_utc(observed_at),
        quality=EvLoadQuality.INCOMPLETE,
        unavailable_active_keys=tuple(sorted(unavailable_keys)),
    )


def normalize_home_load(
    raw_home_load_kw: Any,
    basis: HomeLoadBasis,
    ev_load: ObservedEvLoadSnapshot,
    *,
    at: datetime | None = None,
) -> SitePowerSnapshot:
    """Normalize Home Load once according to its explicit source basis."""
    timestamp = _as_utc(at or ev_load.observed_at)
    try:
        raw_kw = float(raw_home_load_kw)
    except (TypeError, ValueError):
        raw_kw = None
    if raw_kw is not None and not math.isfinite(raw_kw):
        raw_kw = None

    normalized: float | None = None
    quality = ev_load.quality
    if raw_kw is not None and basis == HomeLoadBasis.EXCLUDES_EV:
        normalized = max(0.0, raw_kw)
    elif (
        raw_kw is not None
        and basis == HomeLoadBasis.INCLUDES_EV
        and ev_load.quality == EvLoadQuality.COMPLETE
    ):
        normalized = max(0.0, raw_kw - ev_load.power_kw)
    else:
        quality = EvLoadQuality.INCOMPLETE

    return SitePowerSnapshot(
        raw_home_load_kw=raw_kw,
        raw_home_load_basis=basis,
        observed_ev_load_kw=ev_load.power_kw,
        non_ev_home_load_kw=normalized,
        timestamp=timestamp,
        normalization_quality=quality,
    )


def normalize_energy_data(
    data: dict[str, Any] | None,
    *,
    battery_system: str,
    ev_load: ObservedEvLoadSnapshot,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Return coordinator data with one canonical non-EV Home Load contract.

    Tesla's cloud coordinator has historically removed its own EV scalar,
    while Sigenergy derives an already-adjusted value from site balance.  All
    other coordinator load values are gross site/load telemetry.  Reconstruct
    the raw branch for the two adjusted backends, then normalize once with the
    complete provider-neutral EV snapshot.
    """
    if not isinstance(data, dict):
        return None
    result = dict(data)
    existing_raw = result.get("raw_home_load_power")
    try:
        existing_raw = float(existing_raw) if existing_raw is not None else None
    except (TypeError, ValueError):
        existing_raw = None
    if existing_raw is not None and not math.isfinite(existing_raw):
        existing_raw = None

    try:
        published_load = float(result.get("load_power"))
    except (TypeError, ValueError):
        published_load = existing_raw
    if published_load is None or not math.isfinite(published_load):
        return result

    system = str(battery_system or "").strip().lower()
    if existing_raw is not None:
        raw_load = existing_raw
    elif system == "tesla":
        try:
            embedded_ev = float(result.get("ev_power") or 0.0)
        except (TypeError, ValueError):
            embedded_ev = 0.0
        raw_load = published_load + embedded_ev
    elif system == "sigenergy":
        try:
            raw_load = sum(
                float(result.get(key) or 0.0)
                for key in ("solar_power", "grid_power", "battery_power")
            )
        except (TypeError, ValueError):
            raw_load = published_load
    else:
        raw_load = published_load

    normalized = normalize_home_load(
        raw_load,
        HomeLoadBasis.INCLUDES_EV,
        ev_load,
        at=at,
    )
    result.update(
        {
            "raw_home_load_power": normalized.raw_home_load_kw,
            "home_load_basis": HomeLoadBasis.EXCLUDES_EV.value,
            "observed_ev_power": normalized.observed_ev_load_kw,
            "home_load_normalization_quality": normalized.normalization_quality.value,
        }
    )
    result["load_power"] = normalized.non_ev_home_load_kw
    result["site_load_power"] = max(0.0, raw_load)
    return result
