"""Regression tests for the cross-client settings taxonomy."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "power_sync" / "settings_metadata.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
COORDINATOR_PATH = (
    ROOT / "custom_components" / "power_sync" / "optimization" / "coordinator.py"
)
CONFIG_FLOW_PATH = ROOT / "custom_components" / "power_sync" / "config_flow.py"


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

    assert schema["version"] == 2
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
    assert schema["fields"]["allow_grid_charge"]["owner"] == "optimizer"
    assert schema["fields"]["allow_grid_charge"]["section"] == "grid_site_constraints"
    assert schema["fields"]["max_grid_export_w"]["category"] == "system"
    assert schema["fields"]["max_grid_export_w"]["owner"] == "optimizer"
    assert schema["fields"]["max_grid_charge_price"]["category"] == "advanced"
    assert schema["fields"]["hardware_backup_reserve"]["owner"] == "optimizer"
    assert schema["fields"]["hardware_backup_reserve"]["section"] == "reserve_controls"
    assert schema["fields"]["battery_capacity_wh"]["owner"] == "optimizer"
    assert schema["fields"]["battery_efficiency_learning_enabled"] == {
        "category": "advanced",
        "owner": "optimizer",
        "section": "battery_forecast_inputs",
        "order": 83,
    }
    assert (
        "battery_efficiency_learning_enabled"
        in groups["advanced_optimizer"]["fields"]
    )
    assert schema["fields"]["daily_supply_charge"] == {
        "category": "core",
        "owner": "optimizer",
        "section": "core_goals",
        "order": 12,
    }
    assert schema["fields"]["ev_integration"]["owner"] == "optimizer"
    assert schema["fields"]["spread_export_enabled"]["visible_if"] == {
        "battery_system_not": "tesla"
    }
    # These settings are owned by other endpoints or by HA entity selectors;
    # Schema v2 must not promise that optimization/settings can write them.
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


def test_section_merge_treats_an_omitted_optional_field_as_cleared():
    module = _load_module()

    merged = module.merge_optimization_section_input(
        {
            "max_grid_export_w": 5000,
            "max_grid_import_w": 15000,
            "backup_reserve": 20,
        },
        {"max_grid_export_w", "max_grid_import_w"},
        {"max_grid_import_w": 12000},
    )

    assert merged == {
        "max_grid_import_w": 12000,
        "backup_reserve": 20,
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


def test_auto_apply_reserve_values_split_manual_control_from_applied_floor():
    module = _load_module()

    displayed, applied = module.split_optimizer_reserve_values(
        auto_apply_enabled=True,
        configured_reserve=0.61,
        manual_reserve=0.30,
    )

    assert displayed == 0.30
    assert applied == 0.61

    displayed, applied = module.split_optimizer_reserve_values(
        auto_apply_enabled=False,
        configured_reserve=0.25,
        manual_reserve=0.30,
    )

    assert displayed == 0.25
    assert applied is None


def test_controls_hardware_reserve_uses_one_canonical_persistence_key():
    init_source = INIT_PATH.read_text()
    handler_start = init_source.index("async def handle_set_backup_reserve")
    handler_end = init_source.index(
        "async def handle_set_operation_mode",
        handler_start,
    )
    handler_source = init_source[handler_start:handler_end]

    assert "new_data[CONF_HARDWARE_BACKUP_RESERVE] = hardware_reserve" in handler_source
    assert "new_opts[CONF_HARDWARE_BACKUP_RESERVE] = hardware_reserve" in handler_source
    assert 'new_opts.pop("_user_backup_reserve", None)' in handler_source
    assert '{"_user_backup_reserve": percent}' not in handler_source


def test_legacy_controls_reserve_wins_until_next_canonical_write():
    coordinator_source = COORDINATOR_PATH.read_text()
    method_start = coordinator_source.index(
        "def _configured_startup_backup_reserve"
    )
    method_end = coordinator_source.index(
        "async def _resolve_startup_backup_reserve",
        method_start,
    )
    method_source = coordinator_source[method_start:method_end]

    legacy_index = method_source.index('self._entry.options.get("_user_backup_reserve")')
    canonical_index = method_source.index("hw_reserve =")
    assert legacy_index < canonical_index


def test_ev_planning_participation_is_owned_by_optimizer_options():
    source = CONFIG_FLOW_PATH.read_text()
    ev_step_start = source.index("async def async_step_ev_charging")
    ev_step_end = source.index("async def async_step_zaptec_cloud_options")
    ev_source = source[ev_step_start:ev_step_end]

    assert "CONF_OPTIMIZATION_EV_INTEGRATION" not in ev_source
    assert "CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY" not in ev_source

    optimizer_start = source.index("async def _async_step_optimization")
    optimizer_end = source.index("async def async_step_inverter", optimizer_start)
    optimizer_source = source[optimizer_start:optimizer_end]
    assert "CONF_OPTIMIZATION_EV_INTEGRATION" in optimizer_source
    assert "CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY" in optimizer_source


def test_amber_confidence_is_optional_provider_scoped_and_owned_by_optimizer():
    module = _load_module()
    schema = module.optimizer_settings_schema()
    assert schema['fields']['amber_forecast_confidence_enabled'] == {
        'category': 'advanced', 'owner': 'optimizer',
        'section': 'battery_forecast_inputs', 'order': 84,
        'visible_if': {'electricity_provider': 'amber'},
    }
    assert 'amber_forecast_confidence_enabled' in module.optimizer_settings_groups()['advanced_optimizer']['fields']


def test_paired_local_tesla_metadata_allows_spread_controls():
    module=_load_module()
    default=module.optimizer_settings_schema()
    paired=module.optimizer_settings_schema(local_power_control=True)
    for field in ('spread_export_enabled','spread_import_enabled'):
        assert default['fields'][field]['visible_if']=={'battery_system_not':'tesla'}
        assert paired['fields'][field]['visible_if']=={}


def test_tesla_options_preserve_spread_input_only_with_local_dispatch_capability():
    import ast
    tree=ast.parse(CONFIG_FLOW_PATH.read_text())
    method=next(node for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=='_async_step_optimization')
    assignments=[node for node in ast.walk(method) if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id in ('spread_export_enabled','spread_import_enabled') for t in node.targets)]
    for capability in (True,False):
        ns={'supports_spread':capability,'user_input':{'spread_export_enabled':True,'spread_import_enabled':True},'CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED':'spread_export_enabled','CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED':'spread_import_enabled'}
        for node in assignments:exec(compile(ast.Module(body=[node],type_ignores=[]),'<spread-input>','exec'),ns)
        assert ns['spread_export_enabled'] is capability
        assert ns['spread_import_enabled'] is capability
