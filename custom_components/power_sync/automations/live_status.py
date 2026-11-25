"""Helpers for normalizing coordinator power data for EV automation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
import math


def _native_zero_export(data: dict[str, Any]) -> bool:
    """Recognize an actual site-export register readback, including WiNet-S's floor."""
    for key, scale, enabled in (
        ("export_limit_kw", 1000.0, True),
        ("export_limit_w", 1.0, data.get("export_limit_enabled") is True),
    ):
        value = data.get(key)
        if not enabled or isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        watts = value * scale
        if math.isfinite(watts) and 0 <= watts <= 50:
            return True
    return False


def _kw_to_w(value: Any) -> float:
    """Convert coordinator kW values to watts, treating missing values as 0."""
    try:
        return float(value or 0) * 1000
    except (TypeError, ValueError):
        return 0.0


def _optional_kw_to_w(value: Any) -> float | None:
    """Convert coordinator kW values to watts without inventing missing data."""
    if value is None:
        return None
    try:
        return float(value) * 1000
    except (TypeError, ValueError):
        return None


def coordinator_data_to_ev_live_status(data: dict[str, Any]) -> dict[str, Any]:
    """Convert coordinator data into the EV automation live_status shape.

    TeslaEnergyCoordinator, Sigenergy, Sungrow, and the other site coordinators
    expose power fields in kW. EV automation math expects these fields in watts.
    """
    load_power = data.get("load_power")
    site_load_power = (
        data.get("site_load_power")
        if "site_load_power" in data
        else load_power
    )
    native_zero_export = _native_zero_export(data)
    return {
        "battery_soc": data.get("battery_level", 0),
        "grid_power": _kw_to_w(data.get("grid_power", 0)),
        "solar_power": (
            None if data.get("solar_power_valid") is False
            else _kw_to_w(data.get("solar_power", 0))
        ),
        "battery_power": _kw_to_w(data.get("battery_power", 0)),
        # Home Load deliberately becomes unavailable when an active EV power
        # observation is stale.  Preserve that absence: zero is a real
        # measurement and must not be synthesized for displays or controls.
        "load_power": _optional_kw_to_w(load_power),
        "site_load_power": _optional_kw_to_w(site_load_power),
        "ev_power": _kw_to_w(data.get("ev_power", 0)),
        # EV power as it stood when the site aggregate above was last measured.
        # Absent for coordinators whose telemetry is single-vintage already.
        "ev_power_at_site_sample": _optional_kw_to_w(
            data.get("ev_power_at_site_sample")
        ),
        "home_load_basis": data.get("home_load_basis", "includes_ev"),
        "is_curtailed": bool(data.get("is_curtailed", False)) or native_zero_export,
        "native_zero_export": native_zero_export,
        "ac_production_curtailed": False,
        "site_observed_at": data.get("last_update"),
    }


# --- One site live-status source for EV control, automations and status views ---

# Order matters: the first coordinator with a usable snapshot supplies the site.
SITE_LIVE_STATUS_COORDINATOR_KEYS = (
    "tesla_coordinator",
    "sigenergy_coordinator",
    "sungrow_coordinator",
    "foxess_coordinator",
    "goodwe_coordinator",
    "alphaess_coordinator",
    "esy_sunhome_coordinator",
    "solax_coordinator",
    "saj_h2_coordinator",
    "fronius_reserva_coordinator",
    "neovolt_coordinator",
    "solaredge_coordinator",
    "anker_solix_coordinator",
    "amber_smartshift_coordinator",
    "custom_energy_coordinator",
)


def _stale_after(coordinator: Any) -> timedelta:
    update_interval = getattr(coordinator, "update_interval", None)
    return max(
        update_interval * 4 if update_interval is not None else timedelta(seconds=120),
        timedelta(seconds=60),
    )


def _same_awareness(now: datetime, other: datetime) -> tuple[datetime, datetime]:
    """Give two datetimes the same tz awareness so they can be subtracted."""
    if now.tzinfo is None and other.tzinfo is not None:
        return now.replace(tzinfo=other.tzinfo), other
    if now.tzinfo is not None and other.tzinfo is None:
        return now, other.replace(tzinfo=now.tzinfo)
    return now, other


def coordinator_snapshot_is_usable(coordinator: Any, now: datetime) -> bool:
    """Whether a site coordinator's cached snapshot is safe to steer EV power by.

    The last refresh succeeded, telemetry is ready, grid/solar/battery are
    finite numbers, and the refresh is no older than four update intervals.
    """
    if getattr(coordinator, "last_update_success", None) is not True:
        return False
    data = getattr(coordinator, "data", None)
    if not isinstance(data, dict) or data.get("telemetry_ready") is False:
        return False
    for key in ("grid_power", "solar_power", "battery_power"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        if not math.isfinite(float(value)):
            return False
    last_update = getattr(coordinator, "last_update_success_time", None)
    if last_update is None:
        return True
    try:
        now, last_update = _same_awareness(now, last_update)
        return (now - last_update) <= _stale_after(coordinator)
    except Exception:
        return False


def fronius_snapshot_is_fresh(coordinator: Any, data: Any, now: datetime) -> bool:
    """Keep Fronius load-following behind current site telemetry.

    The Fronius coordinator deliberately retains its last useful payload when
    its upstream integration is unavailable. That is useful for display, but a
    retained load value must never become a new inverter power-limit target.
    Newer bridge snapshots carry per-field source times; load, grid and battery
    must all be current. Older snapshots without that field fall back to the
    coordinator refresh time alone.
    """
    if (
        getattr(coordinator, "last_update_success", False) is not True
        or not isinstance(data, dict)
        or data.get("telemetry_ready") is not True
    ):
        return False
    try:
        stale_after = _stale_after(coordinator)
        last_update = getattr(coordinator, "last_update_success_time", None)
        if last_update is not None:
            aligned_now, last_update = _same_awareness(now, last_update)
            if (aligned_now - last_update) > stale_after:
                return False
        observed_at = data.get("telemetry_observed_at")
        if observed_at is None:
            return True
        if not isinstance(observed_at, dict):
            return False
        for key in ("load_power", "grid_power", "battery_power"):
            source_at = observed_at.get(key)
            if source_at is None:
                return False
            aligned_now, source_at = _same_awareness(now, source_at)
            source_age = aligned_now - source_at
            if source_age > stale_after or source_age < -timedelta(seconds=5):
                return False
        return True
    except Exception:
        return False


def site_live_status(
    entry_data: Mapping[str, Any] | None, now: datetime | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Return ``(live_status, coordinator_configured)`` for the site.

    Uses the first coordinator with a usable snapshot. When a coordinator is
    configured but none is usable, returns ``(None, True)``: callers must not
    fall back to a raw provider API or to retained values.
    """
    now = now or datetime.now(timezone.utc)
    entry_data = entry_data or {}
    configured = False
    for key in SITE_LIVE_STATUS_COORDINATOR_KEYS:
        coordinator = entry_data.get(key)
        if coordinator is None:
            continue
        configured = True
        if not coordinator_snapshot_is_usable(coordinator, now):
            continue
        if key == "fronius_reserva_coordinator" and not fronius_snapshot_is_fresh(
            coordinator, coordinator.data, now,
        ):
            continue
        live_status = coordinator_data_to_ev_live_status(coordinator.data)
        # A separate AC inverter being curtailed curtails the site; a normal AC
        # inverter must not erase a hybrid's native export-limit readback.
        if entry_data.get("inverter_last_state") == "curtailed":
            live_status["is_curtailed"] = True
            live_status["ac_production_curtailed"] = True
        return live_status, True
    return None, configured
