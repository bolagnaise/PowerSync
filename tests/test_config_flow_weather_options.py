"""Regression tests for weather options config-flow schema."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_FLOW_PATH = ROOT / "custom_components" / "power_sync" / "config_flow.py"
STRINGS_PATH = ROOT / "custom_components" / "power_sync" / "strings.json"
TRANSLATIONS_PATH = ROOT / "custom_components" / "power_sync" / "translations" / "en.json"

CONFIG_OPTION_TEXT_STEP_PAIRS = (
    ("provider_selection", "pricing"),
    ("ml_options", "optimization"),
    ("sungrow", "sungrow_connection"),
    ("sungrow", "init_sungrow"),
    ("foxess_connection", "init_foxess"),
    ("foxess_entity", "init_foxess"),
    ("foxess_tcp", "foxess_connection_options"),
    ("foxess_serial", "foxess_connection_options"),
    ("foxess_entity", "foxess_connection_options"),
    ("foxess_tcp", "init_foxess"),
    ("foxess_serial", "init_foxess"),
    ("goodwe_connection", "goodwe_connection_options"),
    ("goodwe_connection", "init_goodwe"),
    ("esy_sunhome", "esy_sunhome_connection"),
    ("saj_h2_battery", "saj_h2_connection"),
    ("fronius_reserva_battery", "fronius_reserva_connection"),
    ("neovolt_battery", "neovolt_connection"),
    ("sigenergy_credentials", "sigenergy_connection"),
    ("sigenergy_station", "sigenergy_connection"),
    ("sigenergy_modbus", "sigenergy_connection"),
    ("sigenergy_dc_curtailment", "sigenergy_connection"),
    ("sigenergy_credentials", "init_sigenergy"),
    ("sigenergy_station", "init_sigenergy"),
    ("sigenergy_modbus", "init_sigenergy"),
    ("sigenergy_dc_curtailment", "init_sigenergy"),
    ("tesla_provider", "tesla_connection"),
    ("site_selection", "tesla_connection"),
    ("tesla_ev_teslemetry_token", "options_tesla_ev_token"),
    ("teslemetry", "teslemetry_token"),
    ("powersync", "powersync_token"),
    ("weather_setup", "weather_options"),
    ("demand_charges", "demand_charge_options"),
    ("curtailment_setup", "curtailment_options"),
    ("sigenergy_dc_curtailment", "curtailment_options"),
    ("weather_setup", "curtailment_options"),
    ("inverter_brand_setup", "inverter_brand"),
    ("inverter_config_setup", "inverter_config"),
    ("solax_battery", "solax_battery_options"),
    ("flow_power_setup", "flow_power_options"),
    ("flow_power_tariff", "flow_power_options"),
    ("flow_power_portal", "flow_power_options"),
    ("flow_power_portal_login", "flow_power_portal_reauth"),
    ("flow_power_portal_mfa", "flow_power_portal_mfa_options"),
    ("amber", "flow_power_amber_token"),
    ("localvolts", "localvolts_options"),
    ("epex", "epex_options"),
    ("octopus", "octopus_options"),
    ("octopus_saving_sessions", "octopus_saving_sessions_options"),
    ("custom_tariff", "custom_tariff_options"),
    ("tariff_period", "tariff_period_options"),
    ("nz_retailer", "nz_options"),
    ("nz_rates", "nz_options"),
    ("globird_plan", "globird_options"),
    ("aemo_config", "globird_options"),
    ("amber_site_selection", "amber_options"),
    ("site_selection", "amber_options"),
    ("amber_settings", "amber_options"),
    ("curtailment_setup", "amber_options"),
    ("inverter_brand_setup", "amber_options"),
    ("inverter_config_setup", "amber_options"),
)


def _module_tree() -> ast.Module:
    return ast.parse(CONFIG_FLOW_PATH.read_text())


def _top_level_function(name: str) -> ast.FunctionDef:
    for node in _module_tree().body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found")


def _config_flow_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _module_tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "PowerSyncConfigFlow":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == name
                ):
                    return item
    raise AssertionError(f"PowerSyncConfigFlow.{name} not found")


def _options_flow_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _module_tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "PowerSyncOptionsFlow":
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == name
                ):
                    return item
    raise AssertionError(f"PowerSyncOptionsFlow.{name} not found")


def _calls_vol_optional_without_default(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if not (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "Optional"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "vol"
    ):
        return False
    if not node.args or not isinstance(node.args[0], ast.Name):
        return False
    return node.args[0].id == "CONF_WEATHER_ENTITY" and not any(
        keyword.arg == "default" for keyword in node.keywords
    )


def test_optional_entity_normalizer_treats_none_as_unset():
    function = _top_level_function("_normalize_optional_entity")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"Any": object}
    exec(compile(module, str(CONFIG_FLOW_PATH), "exec"), namespace)

    normalize = namespace["_normalize_optional_entity"]
    assert normalize(None) is None
    assert normalize("") is None
    assert normalize(" None ") is None
    assert normalize(" weather.forecast_home ") == "weather.forecast_home"


def test_neovolt_capacity_parser_accepts_comma_separated_stack_values():
    function = _top_level_function("_parse_neovolt_capacities_kwh")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"Any": object}
    exec(compile(module, str(CONFIG_FLOW_PATH), "exec"), namespace)

    parse = namespace["_parse_neovolt_capacities_kwh"]

    assert parse("20.1, 30.2", 2) == [20.1, 30.2]
    assert parse("20.1 kWh, 30.2 kWh", 2) == [20.1, 30.2]
    assert parse("20.1, 30.2", 1) == [50.3]
    assert parse("20.1", 2) == [20.1, 20.1]


def test_neovolt_capacity_text_preserves_user_stack_values_for_display():
    function = _top_level_function("_normalize_neovolt_capacities_text")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {"Any": object}
    exec(compile(module, str(CONFIG_FLOW_PATH), "exec"), namespace)

    normalize = namespace["_normalize_neovolt_capacities_text"]

    assert normalize("20.1, 30.2") == "20.1, 30.2"
    assert normalize("20.1 kWh; 30.2 kWh") == "20.1 kWh, 30.2 kWh"
    assert normalize([20.1, 30.2]) == "20.1, 30.2"
    assert normalize("") == ""


def test_neovolt_options_flow_prefers_preserved_capacity_text():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_neovolt_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_NEOVOLT_BATTERY_CAPACITIES_KWH_RAW" in method_source
    assert "_normalize_neovolt_capacities_text" in method_source
    assert (
        "new_data[CONF_NEOVOLT_BATTERY_CAPACITIES_KWH_RAW]"
        in method_source
    )


def test_neovolt_options_flow_returns_updated_options_to_reload_runtime():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_neovolt_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "new_options = dict(self.config_entry.options)" in method_source
    assert (
        "new_options[CONF_NEOVOLT_SURPLUS_BALANCER_MODE] = str("
        in method_source
    )
    assert "return self.async_create_entry(title=\"\", data=new_options)" in method_source


def test_neovolt_surplus_balancer_help_explains_disabled_single_entry_status():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        for section, step_name in (
            ("config", "neovolt_battery"),
            ("options", "neovolt_connection"),
        ):
            description = data[section]["step"][step_name]["data_description"][
                "neovolt_surplus_balancer_mode"
            ]

            assert "multiple selected Neovolt integrations" in description
            assert "Smart Optimization switch does not control it" in description
            assert "Disable" in description


def test_weather_entity_selector_is_conditional_and_has_blank_state():
    method = _options_flow_method("_add_weather_entity_selector")

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_has_weather_entities"
        for node in ast.walk(method)
    )
    assert any(_calls_vol_optional_without_default(node) for node in ast.walk(method))


def test_weather_options_sanitizes_weather_entity_before_storing():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_weather_options")
    method_source = ast.get_source_segment(source, method)

    assert "CONF_WEATHER_ENTITY: _normalize_optional_entity" in method_source
    assert "default=self._get_option(CONF_WEATHER_ENTITY, None)" not in method_source


def test_weather_entity_label_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        step = data["options"]["step"]["weather_options"]

        assert step["data"]["weather_entity"] == "Home Assistant weather entity"
        assert "Optional" in step["data_description"]["weather_entity"]


def test_ev_charging_options_include_fallback_generic_soc_sensor():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_ev_charging")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_GENERIC_CHARGER_SOC_ENTITY" in method_source
    assert "CONF_GENERIC_CHARGER_SOC_ENTITY_2" in method_source
    assert method_source.index("CONF_GENERIC_CHARGER_SOC_ENTITY") < method_source.index(
        "CONF_GENERIC_CHARGER_SOC_ENTITY_2"
    )


def test_ev_charging_save_preserves_fallback_generic_soc_sensor():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("_save_ev_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_GENERIC_CHARGER_SOC_ENTITY_2" in method_source
    assert "final_data[CONF_GENERIC_CHARGER_SOC_ENTITY_2]" in method_source


def test_ev_charging_fallback_generic_soc_sensor_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        for section, step_name in (
            ("options", "ev_charging_setup"),
            ("options", "ev_charging"),
        ):
            step = data[section]["step"][step_name]

            assert (
                step["data"]["generic_charger_soc_entity_2"]
                == "Fallback EV battery SoC sensor"
            )
            assert "primary SoC sensor" in step["data_description"][
                "generic_charger_soc_entity_2"
            ]


def test_globird_initial_flow_warns_tesla_users_about_tariff_baseline():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_aemo_config")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "Tesla Powerwall users only" in method_source
    assert "restart Home Assistant or reload PowerSync" in method_source
    assert "Other battery systems, including" in method_source
    assert "Sigenergy and FoxESS cloud" in method_source
    assert "configure the Globird/TOU custom tariff in" in method_source
    assert '"threshold_hint": threshold_hint' in method_source


def test_globird_options_flow_warns_tesla_users_about_tariff_baseline():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_globird_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "Tesla Powerwall detected" in method_source
    assert "tariff already stored on your Powerwall" in method_source
    assert "restart Home Assistant or reload" in method_source
    assert "PowerSync so the scheduler" in method_source
    assert "Non-Tesla systems, including" in method_source
    assert "Sigenergy and FoxESS cloud" in method_source
    assert "inside PowerSync" in method_source
    assert "feed-in tariff here" in method_source
    assert "your ZeroHero plan here" in method_source


def test_globird_initial_setup_routes_through_plan_selection():
    source = CONFIG_FLOW_PATH.read_text()
    provider_method = _config_flow_method("async_step_provider_selection")
    provider_source = ast.get_source_segment(source, provider_method)
    plan_method = _config_flow_method("async_step_globird_plan")
    plan_source = ast.get_source_segment(source, plan_method)

    assert provider_source is not None
    assert 'provider == "globird"' in provider_source
    assert "return await self.async_step_globird_plan()" in provider_source
    assert plan_source is not None
    assert "GLOBIRD_PLAN_NOT_ZEROHERO" in plan_source
    assert "GLOBIRD_PLAN_ZEROHERO_CUSTOM" in plan_source
    assert "CONF_GLOBIRD_ZEROHERO_IMPORT_LIMIT_KW" in plan_source


def test_globird_plan_strings_are_available_in_setup_and_options():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["globird_plan"]
        options_step = data["options"]["step"]["globird_options"]

        for step in (config_step, options_step):
            assert step["data"]["globird_plan"] == "GloBird ZeroHero plan"
            assert step["data"]["globird_zerohero_export_cap_kwh"] == "Super Export cap"
            assert step["data"]["globird_zerohero_import_limit_kw"] == "No-import threshold"
            assert "15 kWh" in step["data_description"]["globird_plan"]
            assert "0.03 kW" in step["data_description"]["globird_zerohero_import_limit_kw"]


def test_optimization_options_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = optimization_enabled" in method_source
    assert "CONF_OPTIMIZATION_EV_INTEGRATION" in method_source
    assert "new_options[CONF_OPTIMIZATION_EV_INTEGRATION] = ev_integration_enabled" in method_source
    assert "CONF_MONITORING_MODE" in method_source
    assert "new_options[CONF_MONITORING_MODE] = monitoring_mode" in method_source
    assert "CONF_HARDWARE_BACKUP_RESERVE" in method_source
    assert "new_options[CONF_HARDWARE_BACKUP_RESERVE] = hardware_backup_reserve" in method_source
    assert 'new_options.pop("_user_backup_reserve", None)' in method_source
    assert (
        method_source.index("CONF_OPTIMIZATION_BACKUP_RESERVE")
        < method_source.index("CONF_HARDWARE_BACKUP_RESERVE")
        < method_source.index("CONF_OPTIMIZATION_BATTERY_CAPACITY_WH")
    )
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = spread_export_enabled" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED] = spread_import_enabled" in method_source
    assert "CONF_PROFIT_MAX_ENABLED" in method_source
    assert "new_options[CONF_PROFIT_MAX_ENABLED] = profit_max_enabled" in method_source
    assert (
        method_source.index("CONF_PROFIT_MAX_ENABLED")
        < method_source.index("CONF_PROFIT_MAX_TARGET_TIME")
    )
    assert "optimization_provider != OPT_PROVIDER_POWERSYNC" in method_source


def test_neovolt_surplus_balancer_selector_is_in_optimization_options():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "if battery_system == BATTERY_SYSTEM_NEOVOLT:" in method_source
    assert "CONF_NEOVOLT_SURPLUS_BALANCER_MODE" in method_source
    assert "NEOVOLT_SURPLUS_BALANCER_MODES" in method_source
    assert (
        method_source.index("CONF_OPTIMIZATION_ENABLED")
        < method_source.index("CONF_NEOVOLT_SURPLUS_BALANCER_MODE")
        < method_source.index("CONF_OPTIMIZATION_BACKUP_RESERVE")
    )


def test_initial_smart_optimization_configuration_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_ml_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_PROVIDER" in method_source
    assert "self._optimization_provider = optimization_provider" in method_source
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_ENABLED, True)" in method_source
    assert "CONF_OPTIMIZATION_EV_INTEGRATION" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_EV_INTEGRATION, False)" in method_source
    assert "CONF_MONITORING_MODE" in method_source
    assert "user_input.get(CONF_MONITORING_MODE, False)" in method_source
    assert "CONF_HARDWARE_BACKUP_RESERVE" in method_source
    assert (
        method_source.index("CONF_OPTIMIZATION_BACKUP_RESERVE")
        < method_source.index("CONF_HARDWARE_BACKUP_RESERVE")
        < method_source.index("CONF_OPTIMIZATION_BATTERY_CAPACITY_WH")
    )
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "CONF_PROFIT_MAX_ENABLED" in method_source
    assert "user_input.get(CONF_PROFIT_MAX_ENABLED, False)" in method_source
    assert (
        method_source.index("CONF_PROFIT_MAX_ENABLED")
        < method_source.index("CONF_PROFIT_MAX_TARGET_TIME")
    )


def test_initial_setup_routes_to_combined_optimization_options_page():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_battery_system")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "return await self.async_step_ml_options()" in method_source
    assert "return await self.async_step_optimization_provider()" not in method_source


def test_initial_config_flow_does_not_use_options_flow_get_option_helper():
    for node in _module_tree().body:
        if isinstance(node, ast.ClassDef) and node.name == "PowerSyncConfigFlow":
            get_option_calls = [
                item.lineno
                for item in ast.walk(node)
                if (
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Attribute)
                    and item.func.attr == "_get_option"
                    and isinstance(item.func.value, ast.Name)
                    and item.func.value.id == "self"
                )
            ]
            assert get_option_calls == []
            return

    raise AssertionError("PowerSyncConfigFlow class not found")


def test_solaredge_initial_flow_preserves_setup_defaults_locally():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_solaredge")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "current_solaredge = user_input or self._solaredge_data" in method_source
    assert "current_solaredge.get(" in method_source
    assert "CONF_SOLAREDGE_HOST" in method_source
    assert "CONF_SOLAREDGE_PORT" in method_source
    assert "CONF_SOLAREDGE_SLAVE_ID" in method_source
    assert "CONF_SOLAREDGE_RATED_POWER_W" in method_source
    assert "CONF_SOLAREDGE_ENTITY_PREFIX" in method_source
    assert "CONF_SOLAREDGE_DC_CURTAILMENT_ENABLED" in method_source


def test_foxess_initial_flow_offers_cloud_only_backend():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_foxess_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "FOXESS_CONNECTION_CLOUD" in method_source
    assert "FoxESS Cloud API" in method_source
    assert "return await self.async_step_foxess_cloud()" in method_source


def test_foxess_initial_flow_offers_entity_bridge_backend():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_foxess_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "FOXESS_CONNECTION_ENTITY" in method_source
    assert "Entity bridge (foxess_modbus)" in method_source
    assert "return await self.async_step_foxess_entity()" in method_source


def test_foxess_entity_flow_validates_and_stores_bridge_fields():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_foxess_entity")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "_foxess_modbus_entry_options" in method_source
    assert "_validate_foxess_entity_bridge" in method_source
    assert "CONF_FOXESS_ENTITY_CONFIG_ENTRY_ID" in method_source
    assert "CONF_FOXESS_ENTITY_PREFIX" in method_source
    assert "CONF_FOXESS_CONNECTION_TYPE: FOXESS_CONNECTION_ENTITY" in method_source


def test_foxess_options_flows_include_entity_bridge_fields():
    source = CONFIG_FLOW_PATH.read_text()
    for method_name in (
        "async_step_foxess_connection_options",
        "async_step_init_foxess",
    ):
        method = _options_flow_method(method_name)
        method_source = ast.get_source_segment(source, method)

        assert method_source is not None
        assert "FOXESS_CONNECTION_ENTITY" in method_source
        assert "_validate_foxess_entity_bridge" in method_source
        assert "CONF_FOXESS_ENTITY_CONFIG_ENTRY_ID" in method_source
        assert "CONF_FOXESS_ENTITY_PREFIX" in method_source


def test_foxess_cloud_initial_flow_requires_api_key_for_cloud_only():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_foxess_cloud")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "cloud_required" in method_source
    assert "FOXESS_CONNECTION_CLOUD" in method_source
    assert "client.get_device_list()" in method_source
    assert "len(devices) == 1" in method_source
    assert '"foxess_cloud_device_required"' in method_source
    assert '"foxess_cloud_required"' in method_source


def test_foxess_cloud_runtime_uses_battery_system_and_cloud_coordinator():
    init_source = (ROOT / "custom_components" / "power_sync" / "__init__.py").read_text()

    assert "CONF_BATTERY_SYSTEM) == BATTERY_SYSTEM_FOXESS" in init_source
    assert "FOXESS_CONNECTION_CLOUD" in init_source
    assert "FoxESSCloudEnergyCoordinator" in init_source
    assert "Initializing FoxESS Cloud coordinator" in init_source


def test_foxess_entity_runtime_uses_battery_system_and_entity_coordinator():
    init_source = (ROOT / "custom_components" / "power_sync" / "__init__.py").read_text()

    assert "CONF_BATTERY_SYSTEM) == BATTERY_SYSTEM_FOXESS" in init_source
    assert "FOXESS_CONNECTION_ENTITY" in init_source
    assert "FoxESSEntityEnergyCoordinator" in init_source
    assert "Initializing FoxESS entity bridge coordinator" in init_source


def test_goodwe_flow_exposes_explicit_ems_control_mode_selector():
    source = CONFIG_FLOW_PATH.read_text()

    for method_name in (
        "async_step_goodwe_connection",
        "async_step_goodwe_connection_options",
        "async_step_init_goodwe",
    ):
        method = (
            _config_flow_method(method_name)
            if method_name == "async_step_goodwe_connection"
            else _options_flow_method(method_name)
        )
        method_source = ast.get_source_segment(source, method)

        assert method_source is not None
        assert "CONF_GOODWE_EMS_CONTROL_MODE" in method_source
        assert "goodwe_ems_control_options()" in method_source
        assert "validate_goodwe_ems_control_mode" in method_source
        assert "resolve_goodwe_ems_entity_prefix" in method_source
        assert "GOODWE_EMS_CONTROL_ENTITY" in method_source


def test_goodwe_runtime_auto_uses_entity_prefix_for_tcp_control():
    init_source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text()

    assert "CONF_GOODWE_EMS_CONTROL_MODE" in init_source
    assert "GOODWE_EMS_CONTROL_ENTITY" in init_source
    assert "configured_ems_prefix" in init_source
    assert "goodwe_ems_control_mode is None" in init_source
    assert "goodwe_protocol == \"tcp\"" in init_source
    assert "DEFAULT_GOODWE_PORT_TCP" in init_source
    assert "_resolve_goodwe_ems_entity_prefix" in init_source


class _GoodWeStates:
    def __init__(self, entity_ids: list[str]) -> None:
        self._entity_ids = set(entity_ids)

    def async_entity_ids(self, domain: str | None = None) -> list[str]:
        return sorted(
            entity_id
            for entity_id in self._entity_ids
            if domain is None or entity_id.startswith(f"{domain}.")
        )

    def get(self, entity_id: str):
        return object() if entity_id in self._entity_ids else None


class _GoodWeHass:
    def __init__(self, entity_ids: list[str]) -> None:
        self.states = _GoodWeStates(entity_ids)


def _goodwe_prefix_namespace() -> dict[str, object]:
    function_names = {
        "validate_goodwe_ems_entity_prefix",
        "_goodwe_ems_prefix_exists",
        "_goodwe_ems_prefix_candidates",
        "resolve_goodwe_ems_entity_prefix",
        "resolve_goodwe_ems_control_mode",
        "resolve_goodwe_ems_control_mode_for_protocol",
    }
    functions = [
        node
        for node in _module_tree().body
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    ]
    module = ast.Module(body=functions, type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "HomeAssistant": object,
        "_LOGGER": type(
            "_Logger",
            (),
            {"warning": staticmethod(lambda *args, **kwargs: None)},
        ),
        "GOODWE_EMS_CONTROL_DIRECT": "direct",
        "GOODWE_EMS_CONTROL_ENTITY": "entity",
    }
    exec(compile(module, str(CONFIG_FLOW_PATH), "exec"), namespace)
    return namespace


def test_goodwe_ems_prefix_auto_detects_goodwe_when_typed_prefix_is_stale():
    namespace = _goodwe_prefix_namespace()
    resolve_prefix = namespace["resolve_goodwe_ems_entity_prefix"]
    validate_prefix = namespace["validate_goodwe_ems_entity_prefix"]
    hass = _GoodWeHass(
        [
            "select.goodwe_ems_mode",
            "number.goodwe_ems_power_limit",
        ]
    )

    resolved = resolve_prefix(hass, "goodwe_esa")

    assert resolved == "goodwe"
    assert validate_prefix(hass, resolved) is None


def test_goodwe_ems_prefix_keeps_typed_prefix_when_pair_exists():
    namespace = _goodwe_prefix_namespace()
    resolve_prefix = namespace["resolve_goodwe_ems_entity_prefix"]
    hass = _GoodWeHass(
        [
            "select.goodwe_esa_ems_mode",
            "number.goodwe_esa_ems_power_limit",
            "select.goodwe_ems_mode",
            "number.goodwe_ems_power_limit",
        ]
    )

    assert resolve_prefix(hass, "goodwe_esa") == "goodwe_esa"


def test_goodwe_tcp_control_mode_prefers_detected_ems_entities():
    namespace = _goodwe_prefix_namespace()
    resolve_mode = namespace["resolve_goodwe_ems_control_mode_for_protocol"]
    resolve_prefix = namespace["resolve_goodwe_ems_entity_prefix"]
    hass = _GoodWeHass(
        [
            "select.goodwe_esa_ems_mode",
            "number.goodwe_esa_ems_power_limit",
        ]
    )

    prefix = resolve_prefix(hass, "")

    assert prefix == "goodwe_esa"
    assert resolve_mode(hass, "direct", "", "tcp") == "entity"
    assert resolve_mode(hass, "direct", "", "udp") == "direct"


def test_sungrow_options_flow_removes_retired_dual_config():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_sungrow_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "new_options = dict(self.config_entry.options)" in method_source
    assert "self._remove_legacy_sungrow_dual_options(new_data, new_options)" in method_source
    assert "return self.async_create_entry(title=\"\", data=new_options)" in method_source
    assert "CONF_SUNGROW_HOST_2" not in method_source
    assert "CONF_SUNGROW_BATTERY_CAPACITY_2" not in method_source


def test_sungrow_dual_setup_is_not_used_at_runtime():
    source = CONFIG_FLOW_PATH.read_text()
    init_method = _options_flow_method("async_step_init_sungrow")
    init_source = ast.get_source_segment(source, init_method)
    runtime_source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text()

    assert init_source is not None
    assert "CONF_SUNGROW_HOST_2" not in init_source
    assert "DualSungrowCoordinator" not in runtime_source
    assert "sungrow_coordinator_2" not in runtime_source


def test_sungrow_curtailment_options_expose_ac_inverter_path():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_curtailment_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    sungrow_branch = method_source[
        method_source.index("elif is_sungrow:") : method_source.index(
            "else:\n                # Tesla"
        )
    ]

    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in sungrow_branch
    assert "return await self.async_step_inverter_brand()" in sungrow_branch


def test_tesla_curtailment_options_expose_powerwall_offgrid_fallback():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_curtailment_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    tesla_branch = method_source[
        method_source.index("else:\n                # Tesla") : method_source.index(
            "# No AC inverter - route to weather options"
        )
    ]
    tesla_schema_branch = method_source[
        method_source.index("else:\n            # Tesla") : method_source.index(
            "return self.async_show_form"
        )
    ]

    assert "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT" in tesla_branch
    assert "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT" in tesla_schema_branch
    assert "is_tesla = battery_system == BATTERY_SYSTEM_TESLA" in method_source


def test_powerwall_offgrid_fallback_toggle_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        step = data["options"]["step"]["curtailment_options"]

        assert (
            step["data"]["powerwall_offgrid_as_curtailment"]
            == "Enable Powerwall off-grid fallback"
        )
        assert (
            "temporarily island the Powerwall"
            in step["data_description"]["powerwall_offgrid_as_curtailment"]
        )


def test_direct_ac_inverter_menu_enables_runtime_polling():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_inverter_config")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    menu_block = method_source[
        method_source.index('if getattr(self, "_from_menu", False):') :
        method_source.index("final_data[CONF_INVERTER_BRAND]")
    ]

    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in menu_block
    assert "True" in menu_block


def test_sungrow_hybrid_model_can_share_battery_modbus_endpoint():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_inverter_config")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    conflict_index = method_source.index('errors["base"] = "sungrow_modbus_conflict"')
    conflict_block = method_source[conflict_index - 350 : conflict_index + 80]

    assert "inverter_model = user_input.get(CONF_INVERTER_MODEL)" in method_source
    assert 'not str(inverter_model or "").lower().startswith("sh")' in conflict_block


def test_sungrow_ac_inverter_models_include_three_phase_sg_rt():
    const_source = (ROOT / "custom_components" / "power_sync" / "const.py").read_text()
    inverter_source = (
        ROOT / "custom_components" / "power_sync" / "inverters" / "sungrow.py"
    ).read_text()

    assert '"sg10rt": "SG10RT"' in const_source
    assert '"sg10rt": "sg10rs"' in inverter_source


def test_smart_optimization_setup_and_options_text_match():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["ml_options"]
        options_step = data["options"]["step"]["optimization"]

        assert config_step["title"] == options_step["title"]
        assert config_step["description"] == options_step["description"]
        assert config_step["data"] == options_step["data"]
        assert config_step["data_description"] == options_step["data_description"]


def test_config_and_options_flow_shared_text_matches():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_steps = data["config"]["step"]
        option_steps = data["options"]["step"]

        for config_step_name, option_step_name in CONFIG_OPTION_TEXT_STEP_PAIRS:
            if config_step_name not in config_steps or option_step_name not in option_steps:
                continue
            config_step = config_steps[config_step_name]
            option_step = option_steps[option_step_name]

            for section in ("data", "data_description"):
                config_values = config_step.get(section, {})
                option_values = option_step.get(section, {})
                shared_keys = set(config_values) & set(option_values)

                for key in shared_keys:
                    assert option_values[key] == config_values[key], (
                        f"{path.name}: {config_step_name}->{option_step_name} "
                        f"{section}.{key}"
                    )


def test_optimization_enabled_toggle_is_translated_in_config_and_options():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        for section, step_name in (
            ("config", "ml_options"),
            ("options", "optimization"),
        ):
            step = data[section]["step"][step_name]

            assert step["data"]["optimization_enabled"] == "Enable Smart Optimization"
            assert "LP optimizer" in step["data_description"]["optimization_enabled"]
            assert step["data"]["optimization_ev_integration"] == "EV Charging Integration"
            assert "EV charging demand" in step["data_description"]["optimization_ev_integration"]
            assert step["data"]["monitoring_mode"] == "Monitoring mode"
            assert "Block battery and inverter control commands" in step["data_description"]["monitoring_mode"]
            assert step["data"]["hardware_backup_reserve"] == "Hardware backup reserve"
            assert "temporary hold or force-control modes" in step["data_description"]["hardware_backup_reserve"]
            keys = list(step["data"])
            assert keys.index("optimization_backup_reserve") < keys.index("hardware_backup_reserve")
            assert step["data"]["optimization_spread_export_enabled"] == "Spread export across window"
            assert "spreads planned battery export" in step["data_description"]["optimization_spread_export_enabled"]
            assert step["data"]["optimization_spread_import_enabled"] == "Spread import across window"
            assert "spreads planned grid charging" in step["data_description"]["optimization_spread_import_enabled"]
            assert step["data"]["profit_max_enabled"] == "Enable Profit Max"
            assert "profitable export opportunities" in step["data_description"]["profit_max_enabled"]
            assert keys.index("optimization_enabled") < keys.index("optimization_ev_integration")
            assert keys.index("optimization_ev_integration") < keys.index("monitoring_mode")
            assert keys.index("profit_max_enabled") < keys.index("profit_max_target_time")


def test_globird_tariff_guidance_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["aemo_config"]
        options_step = data["options"]["step"]["globird_options"]

        assert "{threshold_hint}" in config_step["description"]
        assert options_step["title"] == "Globird / AEMO settings"
        assert "tariff source" in options_step["description"]
