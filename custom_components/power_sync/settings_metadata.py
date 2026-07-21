"""Shared user-facing settings metadata for PowerSync clients."""

from __future__ import annotations

from typing import Any


def merge_optimization_section_input(
    live_values: dict[str, Any],
    visible_fields: set[str],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    """Merge a section with current hidden values, never a rendered snapshot."""
    return {
        **{
            key: value
            for key, value in live_values.items()
            if key not in visible_fields
        },
        **submitted,
    }


def submitted_live_settings(
    settings: dict[str, Any],
    submitted_fields: set[str],
    form_field_by_setting: dict[str, str],
) -> dict[str, Any]:
    """Return only live optimizer settings owned by the submitted form section."""
    return {
        key: value
        for key, value in settings.items()
        if form_field_by_setting.get(key) in submitted_fields
    }


def optimizer_settings_schema() -> dict[str, Any]:
    """Return cross-client fields writable through optimization/settings.

    Monitoring, Away, and HA entity selectors use separate control surfaces and
    are intentionally not advertised as writable optimizer API fields.
    """
    return {
        "version": 1,
        "fields": {
            "enabled": {"category": "core", "order": 1},
            "profit_max_enabled": {"category": "core", "order": 2},
            "charge_by_time_enabled": {"category": "core", "order": 3},
            "charge_by_time_target_soc": {"category": "core", "order": 4},
            "charge_by_time_target_time": {"category": "core", "order": 5},
            "backup_reserve": {"category": "core", "order": 6},
            "auto_apply_reserve_enabled": {
                "category": "behaviour",
                "order": 10,
            },
            "ev_integration": {
                "category": "behaviour",
                "order": 11,
                "capability": "ev_integration",
            },
            "allow_grid_charge": {
                "category": "behaviour",
                "order": 12,
                "capability": "grid_charge",
            },
            "hardware_backup_reserve": {"category": "system", "order": 20},
            "battery_capacity_wh": {"category": "system", "order": 21},
            "max_charge_w": {"category": "system", "order": 22},
            "max_discharge_w": {"category": "system", "order": 23},
            "max_grid_import_w": {"category": "system", "order": 24},
            "max_grid_export_w": {"category": "system", "order": 25},
            "max_grid_charge_price": {
                "category": "advanced",
                "order": 30,
                "capability": "grid_charge",
            },
            "grid_charge_soc_cap": {
                "category": "advanced",
                "order": 31,
                "capability": "grid_charge",
            },
            "spread_import_enabled": {
                "category": "advanced",
                "order": 32,
                "visible_if": {"battery_system_not": "tesla"},
            },
            "spread_export_enabled": {
                "category": "advanced",
                "order": 33,
                "visible_if": {"battery_system_not": "tesla"},
            },
            "disable_idle_enabled": {
                "category": "advanced",
                "order": 34,
                "capability": "disable_idle",
            },
        },
    }


def optimizer_settings_groups() -> dict[str, Any]:
    """Return the unchanged legacy grouping for existing mobile clients."""
    return {
        "optimizer": {
            "title": "Smart Optimization",
            "collapsed": False,
            "fields": [
                "enabled",
                "backup_reserve",
                "hardware_backup_reserve",
                "profit_max_enabled",
                "charge_by_time_enabled",
                "charge_by_time_target_time",
                "charge_by_time_target_soc",
                "load_entity",
                "planned_ev_load_entity",
                "battery_capacity_wh",
                "max_charge_w",
                "max_discharge_w",
            ],
        },
        "advanced_optimizer": {
            "title": "Advanced optimizer controls",
            "collapsed": True,
            "fields": [
                "allow_grid_charge",
                "max_grid_charge_price",
                "grid_charge_soc_cap",
                "max_grid_import_w",
                "max_grid_export_w",
                "spread_import_enabled",
                "spread_export_enabled",
                "disable_idle_enabled",
                "auto_apply_reserve_enabled",
            ],
        },
    }
