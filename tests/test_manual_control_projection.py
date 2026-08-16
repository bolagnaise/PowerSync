"""Regression coverage for ticket #310 manual-control plan projection."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "power_sync"
    / "optimization"
    / "manual_control.py"
)


@pytest.fixture()
def projection_module():
    spec = importlib.util.spec_from_file_location(
        "power_sync_manual_control_projection",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _timestamps(count: int) -> list[datetime]:
    start = datetime(2026, 8, 16, 3, 10, tzinfo=timezone.utc)
    return [start + timedelta(minutes=5 * idx) for idx in range(count)]


def test_self_consumption_timer_becomes_fixed_manual_slots(projection_module):
    projection = projection_module.build_manual_control_projection(
        {
            "active": True,
            "type": "self_consumption",
            "source": "user",
            "expires_at": _timestamps(1)[0] + timedelta(minutes=12),
        },
        _timestamps(5),
        current_soc=0.60,
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        hardware_reserve=0.10,
        efficiency=1.0,
        interval_minutes=5,
    )

    assert projection is not None
    assert projection.mode_slots == ["self_use", "self_use", "self_use", None, None]
    assert projection.slot_count == 3
    assert projection.status_payload() == {
        "active": True,
        "control_type": "self_consumption",
        "control_source": "manual",
        "projection": "planned",
        "expires_at": "2026-08-16T03:22:00+00:00",
        "projected_slots": 3,
    }


def test_manual_discharge_projection_stops_at_hardware_floor(projection_module):
    projection = projection_module.build_manual_control_projection(
        {
            "active": True,
            "type": "discharge",
            "source": "manual",
            "expires_at": _timestamps(1)[0] + timedelta(minutes=20),
            "power_w": 5_000,
        },
        _timestamps(5),
        current_soc=0.20,
        capacity_wh=10_000,
        max_charge_w=5_000,
        max_discharge_w=5_000,
        hardware_reserve=0.10,
        efficiency=1.0,
        interval_minutes=5,
    )

    assert projection is not None
    assert projection.mode_slots == ["export", "export", "export", "export", None]
    assert projection.required_discharge_kw == pytest.approx([5.0, 5.0, 2.0, 0.0, 0.0])
    assert sum(projection.required_discharge_kw) * (5 / 60) == pytest.approx(1.0)


def test_optimizer_owned_or_expired_control_is_not_projected(projection_module):
    timestamps = _timestamps(2)
    common = {
        "timestamps": timestamps,
        "current_soc": 0.50,
        "capacity_wh": 10_000,
        "max_charge_w": 5_000,
        "max_discharge_w": 5_000,
        "hardware_reserve": 0.10,
        "efficiency": 0.92,
        "interval_minutes": 5,
    }

    assert projection_module.build_manual_control_projection(
        {
            "active": True,
            "type": "charge",
            "source": "optimizer",
            "expires_at": timestamps[-1] + timedelta(minutes=5),
        },
        **common,
    ) is None
    assert projection_module.build_manual_control_projection(
        {
            "active": True,
            "type": "charge",
            "source": "user",
            "expires_at": timestamps[0] - timedelta(seconds=1),
        },
        **common,
    ) is None


def test_schedule_metadata_distinguishes_projection_from_execution():
    module_name = "power_sync_manual_control_schedule_reader"
    spec = importlib.util.spec_from_file_location(
        module_name,
        MODULE_PATH.with_name("schedule_reader.py"),
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    timestamp = _timestamps(1)[0]
    ordinary = module.ScheduleAction(timestamp, "idle", 0.0, 0.5)
    assert ordinary.to_dict() == {
        "timestamp": timestamp.isoformat(),
        "action": "idle",
        "power_w": 0.0,
        "soc": 0.5,
    }

    projected = module.ScheduleAction(
        timestamp,
        "self_consumption",
        1200.0,
        0.49,
        reason="manual_control_projection",
        control_source="manual",
        control_action="self_consumption",
    )
    assert projected.to_dict()["action_reason"] == "manual_control_projection"
    assert projected.to_dict()["control_source"] == "manual"
    assert projected.to_dict()["control_action"] == "self_consumption"
