"""Regression tests for the cross-client settings taxonomy."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "power_sync" / "settings_metadata.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("settings_metadata", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_optimizer_schema_is_versioned_and_legacy_groups_remain_compatible():
    module = _load_module()
    schema = module.optimizer_settings_schema()
    groups = module.optimizer_settings_groups()

    assert schema["version"] == 1
    assert set(groups) == {"optimizer", "advanced_optimizer"}
    assert groups["optimizer"]["collapsed"] is False
    assert groups["advanced_optimizer"]["collapsed"] is True

    grouped_fields = {
        field
        for group in groups.values()
        for field in group["fields"]
    }
    assert grouped_fields - set(schema["fields"]) == {
        "load_entity",
        "planned_ev_load_entity",
    }
    assert "allow_grid_charge" in groups["advanced_optimizer"]["fields"]
    assert schema["fields"]["allow_grid_charge"]["category"] == "behaviour"
    assert schema["fields"]["max_grid_export_w"]["category"] == "system"
    assert schema["fields"]["max_grid_charge_price"]["category"] == "advanced"
    assert schema["fields"]["spread_export_enabled"]["visible_if"] == {
        "battery_system_not": "tesla"
    }
    # These settings are owned by other endpoints or by HA entity selectors;
    # schema v1 must not promise that optimization/settings can write them.
    for field in (
        "monitoring_mode",
        "away_mode",
        "load_entity",
        "planned_ev_load_entity",
    ):
        assert field not in schema["fields"]


def test_section_merge_uses_live_hidden_values_not_rendered_values():
    module = _load_module()
    rendered = {"monitoring_mode": False, "backup_reserve": 20}
    live = {**rendered, "backup_reserve": 35, "max_grid_export_w": 0}

    merged = module.merge_optimization_section_input(
        live,
        {"monitoring_mode"},
        {"monitoring_mode": True},
    )

    assert merged == {
        "monitoring_mode": True,
        "backup_reserve": 35,
        "max_grid_export_w": 0,
    }


def test_behaviour_live_update_cannot_reset_auto_applied_reserve():
    module = _load_module()
    live_settings = {
        "backup_reserve": 0.20,
        "allow_grid_charge": False,
    }

    selected = module.submitted_live_settings(
        live_settings,
        {
            "optimization_auto_apply_reserve",
            "optimization_allow_grid_charge",
            "monitoring_mode",
        },
        {
            "backup_reserve": "optimization_backup_reserve",
            "allow_grid_charge": "optimization_allow_grid_charge",
        },
    )

    assert selected == {"allow_grid_charge": False}
    assert "backup_reserve" not in selected
