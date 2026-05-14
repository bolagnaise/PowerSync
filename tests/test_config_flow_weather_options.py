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
    ("sungrow_secondary", "sungrow_connection"),
    ("sungrow", "init_sungrow"),
    ("sungrow_secondary", "init_sungrow"),
    ("foxess_connection", "init_foxess"),
    ("foxess_tcp", "foxess_connection_options"),
    ("foxess_serial", "foxess_connection_options"),
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

            assert "multiple Neovolt integrations" in description
            assert "one selected integration" in description
            assert "disabled" in description


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


def test_optimization_options_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = optimization_enabled" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = spread_export_enabled" in method_source
    assert "optimization_provider != OPT_PROVIDER_POWERSYNC" in method_source


def test_initial_smart_optimization_configuration_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_ml_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_PROVIDER" in method_source
    assert "self._optimization_provider = optimization_provider" in method_source
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_ENABLED, True)" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source


def test_initial_setup_routes_to_combined_optimization_options_page():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_battery_system")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "return await self.async_step_ml_options()" in method_source
    assert "return await self.async_step_optimization_provider()" not in method_source


def test_foxess_initial_flow_offers_cloud_only_backend():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_foxess_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "FOXESS_CONNECTION_CLOUD" in method_source
    assert "FoxESS Cloud API" in method_source
    assert "return await self.async_step_foxess_cloud()" in method_source


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
            assert step["data"]["optimization_spread_export_enabled"] == "Spread export across window"
            assert "spreads planned battery export" in step["data_description"]["optimization_spread_export_enabled"]


def test_globird_tariff_guidance_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["aemo_config"]
        options_step = data["options"]["step"]["globird_options"]

        assert "{threshold_hint}" in config_step["description"]
        assert options_step["title"] == "Globird / AEMO settings"
        assert "tariff source" in options_step["description"]
