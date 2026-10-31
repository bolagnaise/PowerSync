"""Regression tests for Battery Mode control-source metadata."""

from __future__ import annotations

import ast
import copy
from functools import lru_cache
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "sensor.py"


@lru_cache(maxsize=1)
def _compiled_methods():
    tree = ast.parse(SENSOR_PATH.read_text())
    methods = [
        copy.deepcopy(node)
        for class_node in ast.walk(tree)
        if isinstance(class_node, ast.ClassDef)
        and class_node.name == "BatteryModeSensor"
        for node in class_node.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_get_current_mode", "extra_state_attributes"}
    ]
    for method in methods:
        method.decorator_list = []
    namespace = {
        "Any": object,
        "DOMAIN": "power_sync",
        "BATTERY_MODE_STATE_NORMAL": "normal",
        "BATTERY_MODE_STATE_FORCE_CHARGE": "force_charge",
        "BATTERY_MODE_STATE_FORCE_DISCHARGE": "force_discharge",
        "BATTERY_MODE_STATE_HOLD_SOC": "hold_soc",
        "BATTERY_MODE_STATE_SELF_CONSUMPTION": "self_consumption",
    }
    module = ast.fix_missing_locations(ast.Module(body=methods, type_ignores=[]))
    exec(compile(module, str(SENSOR_PATH), "exec"), namespace)
    return namespace["_get_current_mode"], namespace["extra_state_attributes"]


def _attributes(states: dict) -> dict:
    get_current_mode, extra_state_attributes = _compiled_methods()
    subject = SimpleNamespace(
        hass=SimpleNamespace(data={"power_sync": {"entry": states}}),
        _entry=SimpleNamespace(entry_id="entry"),
    )
    subject._get_current_mode = MethodType(get_current_mode, subject)
    return extra_state_attributes(subject)


@pytest.mark.parametrize(
    ("mode", "state_key", "source", "expected"),
    [
        ("force_charge", "force_charge_state", "Optimizer", "optimizer"),
        ("force_discharge", "force_discharge_state", " USER ", "user"),
        ("hold_soc", "hold_soc_state", "manual", "manual"),
        ("self_consumption", "self_consumption_state", "Automation", "automation"),
        ("force_discharge", "force_discharge_state", "AEMO_SPIKE", "aemo_spike"),
        ("force_charge", "force_charge_state", "future-provider", "future-provider"),
    ],
)
def test_active_mode_exposes_normalized_control_source(
    mode: str,
    state_key: str,
    source: str,
    expected: str,
):
    attrs = _attributes({state_key: {"active": True, "source": source}})

    assert attrs["mode"] == mode
    assert attrs["control_source"] == expected


@pytest.mark.parametrize("source", [None, "", "  ", 123, {"source": "optimizer"}])
def test_missing_empty_or_non_string_source_is_unknown(source):
    attrs = _attributes(
        {"force_charge_state": {"active": True, "source": source}},
    )

    assert attrs["control_source"] == "unknown"


def test_absent_source_is_unknown():
    attrs = _attributes({"force_charge_state": {"active": True}})

    assert attrs["control_source"] == "unknown"


def test_normal_mode_has_unknown_source_even_when_inactive_state_is_stale():
    attrs = _attributes(
        {
            "force_charge_state": {"active": False, "source": "optimizer"},
            "force_discharge_state": {"active": False, "source": "user"},
            "hold_soc_state": {"active": False, "source": "automation"},
            "self_consumption_state": {"active": False, "source": "aemo_spike"},
        }
    )

    assert attrs["mode"] == "normal"
    assert attrs["control_source"] == "unknown"


def test_mode_precedence_uses_source_from_selected_active_state():
    attrs = _attributes(
        {
            "force_charge_state": {"active": True, "source": "User"},
            "force_discharge_state": {"active": True, "source": "optimizer"},
            "hold_soc_state": {"active": True, "source": "automation"},
        }
    )

    assert attrs["mode"] == "force_charge"
    assert attrs["control_source"] == "user"


def test_restore_pending_force_discharge_keeps_source_and_status_metadata():
    attrs = _attributes(
        {
            "force_discharge_state": {
                "active": True,
                "source": "optimizer",
                "command_status": "restore_pending",
            }
        }
    )

    assert attrs["control_source"] == "optimizer"
    assert attrs["command_status"] == "restore_pending"
    assert attrs["physical_effect"] == "unverified"


@pytest.mark.parametrize("command_status", ["pending", "restore_pending"])
def test_local_dispatch_metadata_remains_unchanged_with_source(command_status: str):
    attrs = _attributes(
        {
            "force_charge_state": {
                "active": True,
                "source": "optimizer",
                "powerwall_local_dispatch": True,
                "command_status": command_status,
                "power_w": 1200,
            }
        }
    )

    assert attrs["control_source"] == "optimizer"
    assert attrs["control_path"] == "powerwall_local_dispatch"
    assert attrs["command_status"] == command_status
    assert attrs["physical_effect"] == "unverified"
    assert attrs["battery_power_w"] is None


@pytest.mark.parametrize("command_status", ["pending", "restore_pending"])
def test_native_tariff_metadata_remains_unchanged_with_source(command_status: str):
    attrs = _attributes(
        {
            "force_discharge_state": {
                "active": True,
                "source": "optimizer",
                "native_tariff_force": True,
                "command_status": command_status,
            }
        }
    )

    assert attrs["control_source"] == "optimizer"
    assert attrs["control_path"] == "powerwall_native_tariff"
    assert attrs["command_status"] == command_status
    assert attrs["physical_effect"] == "unverified"
