"""Helpers for normalizing coordinator power data for EV automation."""

from __future__ import annotations

from typing import Any


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
    return {
        "battery_soc": data.get("battery_level", 0),
        "grid_power": _kw_to_w(data.get("grid_power", 0)),
        "solar_power": _kw_to_w(data.get("solar_power", 0)),
        "battery_power": _kw_to_w(data.get("battery_power", 0)),
        # Home Load deliberately becomes unavailable when an active EV power
        # observation is stale.  Preserve that absence: zero is a real
        # measurement and must not be synthesized for displays or controls.
        "load_power": _optional_kw_to_w(load_power),
        "site_load_power": _optional_kw_to_w(site_load_power),
        "ev_power": _kw_to_w(data.get("ev_power", 0)),
        "home_load_basis": data.get("home_load_basis", "includes_ev"),
        "is_curtailed": bool(data.get("is_curtailed", False)),
    }
