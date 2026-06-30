"""Regression tests for PowerSync Solcast settings persistence."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
INIT_PATH = COMPONENT_ROOT / "__init__.py"
CONFIG_FLOW_PATH = COMPONENT_ROOT / "config_flow.py"
COORDINATOR_PATH = COMPONENT_ROOT / "coordinator.py"
OPTIMIZATION_COORDINATOR_PATH = COMPONENT_ROOT / "optimization" / "coordinator.py"
LOAD_ESTIMATOR_PATH = COMPONENT_ROOT / "optimization" / "load_estimator.py"
AUTOMATIONS_PATH = COMPONENT_ROOT / "automations" / "__init__.py"
EV_PLANNER_PATH = COMPONENT_ROOT / "automations" / "ev_charging_planner.py"


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text())


def _top_level_function(path: Path, name: str) -> ast.FunctionDef:
    for node in _module_tree(path).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"Function {name} not found in {path.name}")


def _class_method_source(path: Path, class_name: str, method_name: str) -> str:
    source = path.read_text()
    for node in _module_tree(path).body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    method_source = ast.get_source_segment(source, item)
                    assert method_source is not None
                    return method_source
    raise AssertionError(f"{class_name}.{method_name} not found")


def test_solcast_builtin_config_requires_enabled_key_and_resource_id():
    function = _top_level_function(INIT_PATH, "_solcast_builtin_configured")
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace = {
        "Any": object,
        "CONF_SOLCAST_ENABLED": "solcast_enabled",
        "CONF_SOLCAST_API_KEY": "solcast_api_key",
        "CONF_SOLCAST_RESOURCE_ID": "solcast_resource_id",
    }
    exec(compile(module, str(INIT_PATH), "exec"), namespace)

    configured = namespace["_solcast_builtin_configured"]

    assert configured(
        {
            "solcast_enabled": True,
            "solcast_api_key": "key",
            "solcast_resource_id": "site",
        }
    )
    assert not configured(
        {
            "solcast_enabled": True,
            "solcast_api_key": "",
            "solcast_resource_id": "site",
        }
    )
    assert not configured(
        {
            "solcast_enabled": True,
            "solcast_api_key": "key",
            "solcast_resource_id": " ",
        }
    )
    assert not configured(
        {
            "solcast_enabled": False,
            "solcast_api_key": "key",
            "solcast_resource_id": "site",
        }
    )


def test_mobile_solcast_post_removes_stale_legacy_data_keys():
    method_source = _class_method_source(
        INIT_PATH,
        "WeatherSolcastSettingsView",
        "post",
    )

    assert "new_data = dict(entry.data)" in method_source
    assert "old_effective = {**entry.data, **entry.options}" in method_source
    assert "new_data.pop(CONF_SOLCAST_ENABLED, None)" in method_source
    assert "new_data.pop(CONF_SOLCAST_API_KEY, None)" in method_source
    assert "new_data.pop(CONF_SOLCAST_RESOURCE_ID, None)" in method_source
    assert "new_data.pop(CONF_SOLCAST_ESTIMATE_TYPE, None)" in method_source
    assert "new_data.pop(CONF_SOLAR_FORECAST_PROVIDER, None)" in method_source
    assert "new_effective = {**new_data, **new_options}" in method_source
    assert "_solcast_builtin_configured(new_effective)" in method_source
    assert "update_kwargs[\"data\"] = new_data" in method_source
    assert "_normalize_solar_forecast_provider(" in method_source


def test_mobile_solcast_get_prefers_external_integration_over_builtin_credentials():
    method_source = _class_method_source(
        INIT_PATH,
        "WeatherSolcastSettingsView",
        "get",
    )

    external_check = method_source.index("if external_solcast:")
    builtin_check = method_source.index("elif builtin_configured:")

    assert external_check < builtin_check
    assert 'solcast_source = "integration"' in method_source
    assert '"solcast_estimate_type": opts.get(' in method_source
    assert '"solar_forecast_provider": _normalize_solar_forecast_provider(' in method_source


def test_setup_skips_builtin_solcast_when_external_integration_has_data():
    source = INIT_PATH.read_text()

    assert "solcast_integration_installed = _has_external_solcast_integration(hass)" in source


def test_options_flow_solcast_save_removes_stale_legacy_data_keys():
    method_source = _class_method_source(
        CONFIG_FLOW_PATH,
        "PowerSyncOptionsFlow",
        "async_step_weather_options",
    )

    assert "self._remove_legacy_data_keys" in method_source
    assert "CONF_SOLCAST_ENABLED" in method_source
    assert "CONF_SOLCAST_API_KEY" in method_source
    assert "CONF_SOLCAST_RESOURCE_ID" in method_source
    assert "CONF_SOLCAST_ESTIMATE_TYPE" in method_source
    assert "CONF_SOLAR_FORECAST_PROVIDER" in method_source
    assert "SOLCAST_ESTIMATE_TYPES.items()" in method_source
    assert "SOLAR_FORECAST_PROVIDERS.items()" in method_source
    assert ".strip()" in method_source


def test_solcast_estimate_type_is_used_by_forecast_readers():
    coordinator_source = COORDINATOR_PATH.read_text()
    load_estimator_source = LOAD_ESTIMATOR_PATH.read_text()
    optimization_source = OPTIMIZATION_COORDINATOR_PATH.read_text()

    assert "estimate_type: str = DEFAULT_SOLCAST_ESTIMATE_TYPE" in coordinator_source
    assert 'SOLCAST_ESTIMATE10: ("pv_estimate10", "pv_estimate", "pv_estimate50")' in coordinator_source
    assert "pv_estimate = self._get_pv_estimate(forecast)" in coordinator_source
    assert "estimate_type: str = DEFAULT_SOLCAST_ESTIMATE_TYPE" in load_estimator_source
    assert "provider_preference: str = DEFAULT_SOLAR_FORECAST_PROVIDER" in load_estimator_source
    assert "provider_preference=solar_forecast_provider" in optimization_source
    assert "pv_kw = self._get_pv_estimate(item)" in load_estimator_source
    assert "estimate_type=solcast_estimate_type" in optimization_source


def test_automation_solar_forecast_preserves_legacy_solcast_key():
    state_source = _class_method_source(
        AUTOMATIONS_PATH,
        "AutomationEngine",
        "_async_get_current_state",
    )
    forecast_source = _class_method_source(
        AUTOMATIONS_PATH,
        "AutomationEngine",
        "_async_get_solar_forecast",
    )

    assert 'state["solar_forecast"] = solar_forecast' in state_source
    assert 'state["solcast_forecast"] = solar_forecast' in state_source
    assert 'state["solar_forecast_source"] = solar_forecast.get("source")' in state_source
    assert "provider_preference=provider" in forecast_source
    assert "if not forecast.get(\"source\")" in forecast_source


def test_ev_surplus_forecaster_uses_configured_solar_provider():
    solar_source = _class_method_source(
        EV_PLANNER_PATH,
        "SolarForecaster",
        "get_solar_forecast",
    )
    surplus_init_source = _class_method_source(
        EV_PLANNER_PATH,
        "SurplusForecaster",
        "__init__",
    )
    planner_init_source = _class_method_source(
        EV_PLANNER_PATH,
        "ChargingPlanner",
        "__init__",
    )
    endpoint_source = _class_method_source(
        INIT_PATH,
        "SurplusForecastView",
        "get",
    )

    assert "provider_preference=provider" in solar_source
    assert "SharedSolarForecaster" in solar_source
    assert "self.solar_forecaster = SolarForecaster(hass, config_entry)" in surplus_init_source
    assert "self.surplus_forecaster = SurplusForecaster(hass, config_entry)" in planner_init_source
    assert "SurplusForecaster(self._hass, entry)" in endpoint_source
