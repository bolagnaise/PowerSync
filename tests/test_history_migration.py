"""Tests for Sungrow history relink helpers."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


INIT_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "__init__.py"
)
HISTORY_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "history_migration.py"
)
CONFIG_FLOW_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "config_flow.py"
)
CONF_HISTORY_RELINKS = "history_relinks"


def _load_history_migration():
    pkg = types.ModuleType("power_sync")
    pkg.__path__ = []
    const = types.ModuleType("power_sync.const")
    const.CONF_HISTORY_RELINKS = CONF_HISTORY_RELINKS
    const.DOMAIN = "power_sync"
    previous_pkg = sys.modules.get("power_sync")
    previous_const = sys.modules.get("power_sync.const")
    sys.modules["power_sync"] = pkg
    sys.modules["power_sync.const"] = const
    spec = importlib.util.spec_from_file_location(
        "power_sync.history_migration",
        HISTORY_MIGRATION_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["power_sync.history_migration"] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    if previous_pkg is None:
        sys.modules.pop("power_sync", None)
    else:
        sys.modules["power_sync"] = previous_pkg
    if previous_const is None:
        sys.modules.pop("power_sync.const", None)
    else:
        sys.modules["power_sync.const"] = previous_const
    return module


history_migration = _load_history_migration()
STATUS_ALREADY_LINKED = history_migration.STATUS_ALREADY_LINKED
STATUS_AMBIGUOUS = history_migration.STATUS_AMBIGUOUS
STATUS_BLOCKED_COLLISION = history_migration.STATUS_BLOCKED_COLLISION
STATUS_MISSING_NEW = history_migration.STATUS_MISSING_NEW
STATUS_MISSING_OLD = history_migration.STATUS_MISSING_OLD
STATUS_READY = history_migration.STATUS_READY
apply_history_relink_for_registry = history_migration.apply_history_relink_for_registry
history_relink_applied_for_key = history_migration.history_relink_applied_for_key
preview_history_relink_for_registry = history_migration.preview_history_relink_for_registry


class _Registry:
    def __init__(self, entries: list[SimpleNamespace]) -> None:
        self.entities = {entry.entity_id: entry for entry in entries}

    def async_get(self, entity_id: str):
        return self.entities.get(entity_id)

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str):
        for entity in self.entities.values():
            if (
                entity.entity_id.startswith(f"{domain}.")
                and entity.platform == platform
                and entity.unique_id == unique_id
            ):
                return entity.entity_id
        return None

    def async_update_entity(self, entity_id: str, *, new_entity_id: str) -> None:
        entity = self.entities.pop(entity_id)
        entity.entity_id = new_entity_id
        entity.domain = new_entity_id.split(".", 1)[0]
        self.entities[new_entity_id] = entity


def _entity(
    entity_id: str,
    unique_id: str,
    platform: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        entity_id=entity_id,
        unique_id=unique_id,
        platform=platform,
        domain=entity_id.split(".", 1)[0],
    )


def test_preview_detects_ready_mkaiser_to_powersync_mapping():
    registry = _Registry(
        [
            _entity("sensor.daily_pv_generation", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.power_sync_daily_solar_energy", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = preview_history_relink_for_registry(registry, "entry-1")
    solar = result["mappings"][0]

    assert solar["status"] == STATUS_READY
    assert solar["old_entity_id"] == "sensor.daily_pv_generation"
    assert solar["new_entity_id"] == "sensor.power_sync_daily_solar_energy"
    assert result["ready_count"] == 1


def test_preview_reports_missing_new_and_missing_old():
    registry = _Registry(
        [
            _entity("sensor.daily_pv_generation", "sg_daily_pv_generation", "modbus"),
        ]
    )

    result = preview_history_relink_for_registry(registry, "entry-1")

    assert result["mappings"][0]["status"] == STATUS_MISSING_NEW
    assert result["mappings"][1]["status"] == STATUS_MISSING_OLD


def test_preview_reports_ambiguous_old_candidates():
    registry = _Registry(
        [
            _entity("sensor.some_daily_pv", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.daily_pv_generation", "other_unique_id", "template"),
            _entity("sensor.power_sync_daily_solar_energy", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = preview_history_relink_for_registry(registry, "entry-1")

    assert result["mappings"][0]["status"] == STATUS_AMBIGUOUS
    assert sorted(result["mappings"][0]["old_candidates"]) == [
        "sensor.daily_pv_generation",
        "sensor.some_daily_pv",
    ]


def test_preview_blocks_legacy_collision():
    registry = _Registry(
        [
            _entity("sensor.daily_pv_generation", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.daily_pv_generation_legacy", "old_legacy", "template"),
            _entity("sensor.power_sync_daily_solar_energy", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = preview_history_relink_for_registry(registry, "entry-1")

    assert result["mappings"][0]["status"] == STATUS_BLOCKED_COLLISION


def test_apply_relinks_ready_entities_and_records_options():
    registry = _Registry(
        [
            _entity("sensor.daily_pv_generation", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.power_sync_daily_solar_energy", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = apply_history_relink_for_registry(registry, "entry-1", {})

    assert result["applied_count"] == 1
    assert registry.async_get("sensor.daily_pv_generation").unique_id == "entry-1_daily_solar_energy"
    assert registry.async_get("sensor.daily_pv_generation_legacy").unique_id == "sg_daily_pv_generation"
    assert (
        result["history_relinks"]["daily_solar_energy"]["entity_id"]
        == "sensor.daily_pv_generation"
    )
    assert history_relink_applied_for_key(
        {CONF_HISTORY_RELINKS: result["history_relinks"]},
        "daily_solar_energy",
    )


def test_preview_reports_already_linked_after_apply():
    registry = _Registry(
        [
            _entity("sensor.daily_pv_generation_legacy", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.daily_pv_generation", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = preview_history_relink_for_registry(registry, "entry-1")

    assert result["mappings"][0]["status"] == STATUS_ALREADY_LINKED


def test_apply_preserves_custom_old_entity_id_in_stored_preview():
    registry = _Registry(
        [
            _entity("sensor.sungrow_solar_today", "sg_daily_pv_generation", "modbus"),
            _entity("sensor.power_sync_daily_solar_energy", "entry-1_daily_solar_energy", "power_sync"),
        ]
    )

    result = apply_history_relink_for_registry(registry, "entry-1", {})
    relinks = result["history_relinks"]
    preview = preview_history_relink_for_registry(
        registry,
        "entry-1",
        {CONF_HISTORY_RELINKS: relinks},
    )

    assert registry.async_get("sensor.sungrow_solar_today").unique_id == "entry-1_daily_solar_energy"
    assert registry.async_get("sensor.sungrow_solar_today_legacy").unique_id == "sg_daily_pv_generation"
    assert preview["mappings"][0]["status"] == STATUS_ALREADY_LINKED


def test_canonical_entity_id_migration_checks_history_relinks():
    source = INIT_PATH.read_text()

    assert "history_relink_applied_for_key(entry.options, key)" in source


def test_history_relink_menu_is_sungrow_only():
    source = CONFIG_FLOW_PATH.read_text()
    sungrow_branch = source.index("elif battery_system == BATTERY_SYSTEM_SUNGROW:")
    foxess_branch = source.index("elif battery_system == BATTERY_SYSTEM_FOXESS:")

    assert "menu_options.append(\"history_relink\")" in source[sungrow_branch:foxess_branch]
