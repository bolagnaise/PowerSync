"""Regression tests for weather options config-flow schema."""

from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_FLOW_PATH = ROOT / "custom_components" / "power_sync" / "config_flow.py"
STRINGS_PATH = ROOT / "custom_components" / "power_sync" / "strings.json"
TRANSLATIONS_PATH = ROOT / "custom_components" / "power_sync" / "translations" / "en.json"


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
    assert "optimization_provider != OPT_PROVIDER_POWERSYNC" in method_source


def test_initial_smart_optimization_configuration_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_ml_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_ENABLED, True)" in method_source


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


def test_globird_tariff_guidance_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["aemo_config"]
        options_step = data["options"]["step"]["globird_options"]

        assert "{threshold_hint}" in config_step["description"]
        assert options_step["title"] == "Globird / AEMO settings"
        assert "tariff source" in options_step["description"]
