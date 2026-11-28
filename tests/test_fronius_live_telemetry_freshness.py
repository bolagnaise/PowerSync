"""Regression coverage for Fronius curtailment live-status freshness."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace


LIVE_STATUS_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components" / "power_sync" / "automations" / "live_status.py"
)


def _fronius_snapshot_is_fresh():
    """Load the shared Fronius guard without importing the integration."""
    spec = importlib.util.spec_from_file_location("_live_status_under_test", LIVE_STATUS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return lambda coordinator, data: module.fronius_snapshot_is_fresh(
        coordinator, data, datetime.now(timezone.utc),
    )


def test_fronius_successful_ready_snapshot_without_optional_timestamp_is_usable():
    """Ticket #24: do not defer curtailment on HA runtimes without that field."""
    coordinator = SimpleNamespace(
        last_update_success=True,
        data={"telemetry_ready": True},
        update_interval=timedelta(seconds=30),
    )

    assert _fronius_snapshot_is_fresh()(coordinator, coordinator.data) is True


def test_fronius_unsuccessful_or_not_ready_snapshot_remains_rejected():
    is_fresh = _fronius_snapshot_is_fresh()

    assert is_fresh(SimpleNamespace(last_update_success=False), {"telemetry_ready": True}) is False
    assert is_fresh(SimpleNamespace(last_update_success=True), {"telemetry_ready": False}) is False


def _fronius_observed_data(observed_at: dict[str, datetime]) -> dict:
    return {
        "telemetry_ready": True,
        "telemetry_observed_at": observed_at,
        "load_power": 1.2,
        "grid_power": -0.4,
        "battery_power": 0.0,
    }


def test_fronius_source_observation_must_be_fresh_for_live_control():
    is_fresh = _fronius_snapshot_is_fresh()
    now = datetime.now(timezone.utc)
    coordinator = SimpleNamespace(
        last_update_success=True,
        last_update_success_time=now,
        update_interval=timedelta(seconds=30),
    )

    for stale_field in ("load_power", "grid_power", "battery_power"):
        observed_at = {
            "load_power": now,
            "grid_power": now,
            "battery_power": now,
        }
        observed_at[stale_field] = now - timedelta(hours=1)
        assert is_fresh(coordinator, _fronius_observed_data(observed_at)) is False


def test_fronius_fresh_source_observations_are_usable_without_coordinator_timestamp():
    is_fresh = _fronius_snapshot_is_fresh()
    now = datetime.now(timezone.utc)
    coordinator = SimpleNamespace(
        last_update_success=True,
        data=None,
        update_interval=timedelta(seconds=30),
    )

    assert is_fresh(
        coordinator,
        _fronius_observed_data(
            {
                "load_power": now,
                "grid_power": now,
                "battery_power": now,
            }
        ),
    ) is True


def test_fronius_partial_source_provenance_fails_closed():
    is_fresh = _fronius_snapshot_is_fresh()
    now = datetime.now(timezone.utc)
    coordinator = SimpleNamespace(
        last_update_success=True,
        last_update_success_time=now,
        update_interval=timedelta(seconds=30),
    )

    assert is_fresh(
        coordinator,
        _fronius_observed_data({"load_power": now, "grid_power": now}),
    ) is False
