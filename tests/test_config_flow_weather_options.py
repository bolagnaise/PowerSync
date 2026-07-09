"""Regression tests for weather options config-flow schema."""

from __future__ import annotations

import ast
import json
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONFIG_FLOW_PATH = ROOT / "custom_components" / "power_sync" / "config_flow.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"
SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "sensor.py"
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
    ("flow_power_site", "flow_power_site_options"),
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


def _top_level_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _module_tree().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
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


def _init_class_method(
    class_name: str, method_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(INIT_PATH.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    return item
    raise AssertionError(f"{class_name}.{method_name} not found")


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


def test_fronius_gen24_storage_keeps_legacy_step_ids_and_routes():
    route = ast.get_source_segment(
        CONFIG_FLOW_PATH.read_text(),
        _config_flow_method("_route_to_battery_setup"),
    )
    create_entry = ast.get_source_segment(
        CONFIG_FLOW_PATH.read_text(),
        _config_flow_method("_create_final_entry"),
    )

    assert route is not None
    assert create_entry is not None
    assert "BATTERY_SYSTEM_FRONIUS_RESERVA" in route
    assert "return await self.async_step_fronius_reserva_battery()" in route
    assert '"_fronius_reserva_data"' in create_entry
    assert ("fronius_reserva_battery", "fronius_reserva_connection") in CONFIG_OPTION_TEXT_STEP_PAIRS


def test_fronius_gen24_storage_strings_are_generic():
    strings = json.loads(STRINGS_PATH.read_text())
    translations = json.loads(TRANSLATIONS_PATH.read_text())

    for payload in (strings, translations):
        config_steps = payload["config"]["step"]
        options_steps = payload["options"]["step"]
        errors = payload["config"]["error"]
        aborts = payload["config"]["abort"]

        assert config_steps["fronius_reserva_battery"]["title"] == "Fronius GEN24 storage connection"
        assert options_steps["fronius_reserva_connection"]["title"] == "Fronius GEN24 storage connection"
        assert "GEN24 BYD or Reserva storage" in config_steps["fronius_reserva_battery"]["description"]
        assert "Fronius GEN24 storage entities" in errors["fronius_reserva_missing_entities"]
        assert "Fronius GEN24 storage entities" in errors["fronius_reserva_connect_failed"]
        assert "GEN24 BYD or Reserva storage" in aborts["fronius_reserva_not_installed"]


def test_fronius_gen24_storage_flow_validates_fronius_modbus_entry():
    source = CONFIG_FLOW_PATH.read_text()
    setup = ast.get_source_segment(source, _config_flow_method("async_step_fronius_reserva_battery"))
    options = ast.get_source_segment(source, _options_flow_method("async_step_fronius_reserva_connection"))

    assert setup is not None
    assert options is not None
    for method_source in (setup, options):
        assert 'async_entries("fronius_modbus")' in method_source
        assert 'async_abort(reason="fronius_reserva_not_installed")' in method_source
        assert "await ctrl.connect()" in method_source
        assert 'errors["base"] = "fronius_reserva_missing_entities"' in method_source
        assert 'errors["base"] = "fronius_reserva_connect_failed"' in method_source


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
    source = CONFIG_FLOW_PATH.read_text()
    method_source = ast.get_source_segment(source, method)

    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_has_weather_entities"
        for node in ast.walk(method)
    )
    assert any(_calls_vol_optional_without_default(node) for node in ast.walk(method))
    assert method_source is not None
    assert "suggested_value" in method_source
    assert "default=current_weather_entity" not in method_source


def test_weather_options_sanitizes_weather_entity_before_storing():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_weather_options")
    method_source = ast.get_source_segment(source, method)

    assert "CONF_WEATHER_ENTITY: _normalize_optional_entity" in method_source
    assert "default=self._get_option(CONF_WEATHER_ENTITY, None)" not in method_source


def test_weather_options_include_solar_forecast_provider_selector():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_weather_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_SOLAR_FORECAST_PROVIDER" in method_source
    assert "DEFAULT_SOLAR_FORECAST_PROVIDER" in method_source
    assert "SOLAR_FORECAST_PROVIDERS.items()" in method_source
    assert "SelectSelectorMode.DROPDOWN" in method_source


def test_weather_entity_label_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        step = data["options"]["step"]["weather_options"]

        assert step["data"]["weather_entity"] == "Home Assistant weather entity"
        assert "Optional" in step["data_description"]["weather_entity"]
        assert step["data"]["solar_forecast_provider"] == "Solar forecast provider"
        assert "falls back" in step["data_description"]["solar_forecast_provider"]


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


def test_ev_charging_save_allows_clearing_generic_charger_entities():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("_save_ev_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    for key in (
        "CONF_GENERIC_CHARGER_SWITCH_ENTITY",
        "CONF_GENERIC_CHARGER_AMPS_ENTITY",
        "CONF_GENERIC_CHARGER_STATUS_ENTITY",
        "CONF_GENERIC_CHARGER_POWER_ENTITY",
        "CONF_GENERIC_CHARGER_SOC_ENTITY",
        "CONF_GENERIC_CHARGER_SOC_ENTITY_2",
    ):
        assert f"final_data[{key}] = ev_input.get(" in method_source
    for stale_guard in (
        "if generic_switch:",
        "if generic_amps:",
        "if generic_status:",
        "if generic_power:",
        "if generic_soc:",
        "if generic_soc_2:",
    ):
        assert stale_guard not in method_source


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
            assert (
                step["data"]["generic_charger_power_entity"]
                == "EV charging power sensor"
            )
            assert "measured EV charging power" in step["data_description"][
                "generic_charger_power_entity"
            ]
            assert "primary SoC sensor" in step["data_description"][
                "generic_charger_soc_entity_2"
            ]


def test_ev_charging_sigenergy_charger_fields_are_translated():
    sigenergy_keys = (
        "sigenergy_charger_enabled",
        "sigenergy_charger_type",
        "sigenergy_charger_host",
        "sigenergy_charger_port",
        "sigenergy_charger_slave_id",
        "sigenergy_charger_charge_power_limit_entity",
        "sigenergy_charger_discharge_power_limit_entity",
    )

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        for step_name in ("ev_charging_setup", "ev_charging"):
            step = data["options"]["step"][step_name]

            for key in sigenergy_keys:
                assert key in step["data"], f"{path.name}: {step_name}.data.{key}"
                assert key in step["data_description"], (
                    f"{path.name}: {step_name}.data_description.{key}"
                )
            assert step["data"]["sigenergy_charger_enabled"] == (
                "Enable Sigenergy EV charger"
            )
            assert "EVAC/EVDC" in step["data_description"][
                "sigenergy_charger_enabled"
            ]


def test_ev_charging_sigenergy_charger_slave_id_allows_247():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_ev_charging")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_SIGENERGY_CHARGER_SLAVE_ID" in method_source
    assert "min=1, max=247, step=1" in method_source


def test_vehicle_config_creation_preserves_sigenergy_fields():
    source = INIT_PATH.read_text()
    method = _init_class_method("VehicleChargingConfigView", "post")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    for key in (
        "sigenergy_charger_host",
        "sigenergy_charger_port",
        "sigenergy_charger_slave_id",
        "sigenergy_charger_type",
        "sigenergy_charger_charge_power_limit_entity",
        "sigenergy_charger_discharge_power_limit_entity",
    ):
        assert key in method_source


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


def test_globird_options_flow_has_plan_schema_helper():
    source = CONFIG_FLOW_PATH.read_text()
    helper = _options_flow_method("_globird_plan_schema")
    helper_source = ast.get_source_segment(source, helper)
    method = _options_flow_method("async_step_globird_options")
    method_source = ast.get_source_segment(source, method)

    assert helper_source is not None
    assert "_build_globird_plan_schema" in helper_source
    assert "rate_unit=self._selector_unit()" in helper_source
    assert "currency_unit=self._currency()" in helper_source
    assert method_source is not None
    assert "self._globird_plan_schema(current_globird_settings).schema" in method_source


def test_other_custom_tou_options_route_directly_to_custom_tariff():
    source = CONFIG_FLOW_PATH.read_text()
    route_helper = ast.get_source_segment(
        source,
        _options_flow_method("_async_route_custom_tou_options"),
    )
    pricing_step = ast.get_source_segment(
        source,
        _options_flow_method("async_step_pricing"),
    )
    provider_router = ast.get_source_segment(
        source,
        _options_flow_method("_async_route_to_provider_options"),
    )

    assert route_helper is not None
    assert 'provider in ("other", "tou_only")' in route_helper
    assert "return await self.async_step_custom_tariff_options()" in route_helper
    assert "return await self.async_step_globird_options()" in route_helper

    assert pricing_step is not None
    assert "return await self._async_route_custom_tou_options(provider)" in pricing_step

    assert provider_router is not None
    assert "return await self._async_route_custom_tou_options(provider)" in provider_router


def test_other_custom_tou_initial_setup_routes_directly_to_custom_tariff():
    source = CONFIG_FLOW_PATH.read_text()
    provider_step = ast.get_source_segment(
        source,
        _config_flow_method("async_step_provider_selection"),
    )
    custom_step = ast.get_source_segment(
        source,
        _config_flow_method("async_step_custom_tariff"),
    )
    period_step = ast.get_source_segment(
        source,
        _config_flow_method("async_step_tariff_period"),
    )
    create_entry = ast.get_source_segment(
        source,
        _config_flow_method("_create_final_entry"),
    )

    assert provider_step is not None
    assert 'provider == "other"' in provider_step
    assert "return await self.async_step_custom_tariff()" in provider_step
    assert "return await self.async_step_aemo_config()" not in provider_step[
        provider_step.index('provider == "other"') :
    ]

    assert custom_step is not None
    assert 'step_id="custom_tariff"' in custom_step
    assert "self._custom_tariff_data = self._build_tariff_from_periods" in custom_step
    assert "return await self.async_step_battery_system()" in custom_step
    assert "skip_tariff" in custom_step

    assert period_step is not None
    assert 'step_id="tariff_period"' in period_step
    assert "self._custom_tariff_data = self._build_tariff_from_periods" in period_step

    assert create_entry is not None
    assert 'data["initial_custom_tariff"] = self._custom_tariff_data' in create_entry
    assert 'title = "PowerSync Custom TOU"' in create_entry


def test_custom_tou_tariff_periods_support_weekend_only_ranges():
    source = CONFIG_FLOW_PATH.read_text()
    initial_period_step = ast.get_source_segment(
        source,
        _config_flow_method("async_step_tariff_period"),
    )
    options_period_step = ast.get_source_segment(
        source,
        _options_flow_method("async_step_tariff_period_options"),
    )
    builder_source = ast.get_source_segment(
        source,
        _config_flow_method("_build_tariff_from_periods"),
    )

    assert initial_period_step is not None
    assert options_period_step is not None
    assert builder_source is not None
    assert '"weekends": "Weekends only (Sat-Sun)"' in initial_period_step
    assert '"weekends": "Weekends only (Sat-Sun)"' in options_period_step

    namespace = {
        "normalize_currency": lambda value, fallback: value or fallback,
        "currency_for_provider": lambda provider, hass: "AUD",
    }
    exec(textwrap.dedent(builder_source), namespace)

    ctx = type(
        "Ctx",
        (),
        {
            "_tariff_offpeak_rate": 0.15,
            "_tariff_fit_rate": 0.05,
            "_tariff_plan_name": "Red Energy TOU",
            "_selected_electricity_provider": "other",
            "_tariff_currency": None,
            "hass": None,
        },
    )()

    tariff = namespace["_build_tariff_from_periods"](
        ctx,
        [
            {
                "name": "PEAK",
                "start": 14,
                "end": 20,
                "days": "weekdays",
                "import_rate": 0.48,
                "export_rate": 0.04,
            },
            {
                "name": "SHOULDER",
                "start": 7,
                "end": 22,
                "days": "weekends",
                "import_rate": 0.28,
                "export_rate": 0.04,
            },
        ],
    )

    tou_periods = tariff["seasons"]["All Year"]["tou_periods"]

    assert {
        "fromDayOfWeek": 0,
        "toDayOfWeek": 0,
        "fromHour": 7,
        "toHour": 22,
    } in tou_periods["SHOULDER"]
    assert {
        "fromDayOfWeek": 6,
        "toDayOfWeek": 6,
        "fromHour": 7,
        "toHour": 22,
    } in tou_periods["SHOULDER"]
    assert {
        "fromDayOfWeek": 1,
        "toDayOfWeek": 5,
        "fromHour": 0,
        "toHour": 14,
    } in tou_periods["OFF_PEAK"]
    assert {
        "fromDayOfWeek": 1,
        "toDayOfWeek": 5,
        "fromHour": 20,
        "toHour": 24,
    } in tou_periods["OFF_PEAK"]
    assert {
        "fromDayOfWeek": 0,
        "toDayOfWeek": 0,
        "fromHour": 22,
        "toHour": 24,
    } in tou_periods["OFF_PEAK"]
    assert {
        "fromDayOfWeek": 6,
        "toDayOfWeek": 6,
        "fromHour": 0,
        "toHour": 7,
    } in tou_periods["OFF_PEAK"]


def test_globird_plan_strings_are_available_in_setup_and_options():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["globird_plan"]
        options_step = data["options"]["step"]["globird_options"]

        for step in (config_step, options_step):
            assert step["data"]["globird_plan"] == "GloBird ZeroHero plan"
            assert step["data"]["globird_zerohero_export_cap_kwh"] == "Super Export cap"
            assert step["data"]["globird_zerohero_import_limit_kw"] == "No-import threshold"
            assert step["data"]["globird_zerocharge_start"] == "Custom ZeroCharge start"
            assert step["data"]["globird_zerocharge_end"] == "Custom ZeroCharge end"
            assert step["data"]["globird_zerocharge_import_cap_kwh"] == "ZeroCharge import cap"
            assert "Jul 2026" in step["data_description"]["globird_plan"]
            assert "15 kWh" in step["data_description"]["globird_plan"]
            assert "12:00" in step["data_description"]["globird_zerocharge_start"]
            assert "15:00" in step["data_description"]["globird_zerocharge_end"]
            assert "0.09 kWh total import allowance" in step["data_description"]["globird_zerohero_import_limit_kw"]


def test_globird_plan_schema_exposes_jul_2026_and_zerocharge_fields():
    source = CONFIG_FLOW_PATH.read_text()
    helper = ast.get_source_segment(source, _top_level_function("_build_globird_plan_schema"))
    setup_step = ast.get_source_segment(
        source, _config_flow_method("async_step_globird_plan")
    )
    options_step = ast.get_source_segment(
        source, _options_flow_method("async_step_globird_options")
    )
    api_source = INIT_PATH.read_text()

    assert helper is not None
    assert setup_step is not None
    assert options_step is not None
    assert "GLOBIRD_PLAN_ZEROHERO_JUL_2026" in CONST_PATH.read_text()
    assert "ZeroHero Jul 2026" in CONST_PATH.read_text()
    assert "CONF_GLOBIRD_ZEROCHARGE_START" in helper
    assert "CONF_GLOBIRD_ZEROCHARGE_END" in helper
    assert "CONF_GLOBIRD_ZEROCHARGE_IMPORT_CAP_KWH" in helper
    assert "CONF_GLOBIRD_ZEROCHARGE_START" in setup_step
    assert "CONF_GLOBIRD_ZEROCHARGE_END" in setup_step
    assert "CONF_GLOBIRD_ZEROCHARGE_IMPORT_CAP_KWH" in setup_step
    assert "CONF_GLOBIRD_ZEROCHARGE_START" in options_step
    assert "CONF_GLOBIRD_ZEROCHARGE_END" in options_step
    assert "CONF_GLOBIRD_ZEROCHARGE_IMPORT_CAP_KWH" in options_step
    assert '"globird_zerocharge_start": CONF_GLOBIRD_ZEROCHARGE_START' in api_source
    assert '"globird_zerocharge_end": CONF_GLOBIRD_ZEROCHARGE_END' in api_source
    assert (
        '"globird_zerocharge_import_cap_kwh": CONF_GLOBIRD_ZEROCHARGE_IMPORT_CAP_KWH'
        in api_source
    )


def test_provider_portal_login_has_dedicated_options_sections():
    source = CONFIG_FLOW_PATH.read_text()
    init_options = ast.get_source_segment(
        source, _options_flow_method("async_step_init")
    )
    provider_portal = ast.get_source_segment(
        source, _options_flow_method("async_step_provider_portal")
    )
    flow_options = ast.get_source_segment(
        source, _options_flow_method("async_step_flow_power_options")
    )
    globird_options = ast.get_source_segment(
        source, _options_flow_method("async_step_globird_options")
    )

    assert init_options is not None
    assert provider_portal is not None
    assert flow_options is not None
    assert globird_options is not None
    assert "provider_portal" in init_options
    assert "async_step_flow_power_portal_options()" in provider_portal
    assert "async_step_globird_portal_options()" in provider_portal
    assert "configure_flow_power_portal" not in flow_options
    assert "CONF_FLOWPOWER_EMAIL" not in flow_options
    assert "CONF_FLOWPOWER_PASSWORD" not in flow_options
    assert "configure_globird_portal" not in globird_options
    assert "CONF_GLOBIRD_EMAIL" not in globird_options
    assert "CONF_GLOBIRD_PASSWORD" not in globird_options

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        options_steps = data["options"]["step"]
        menu_options = options_steps["init"]["menu_options"]

        assert menu_options["provider_portal"] == "Provider portal login"

        flow_step = options_steps["flow_power_options"]
        assert "Provider portal login page" in flow_step["description"]
        assert "configure_flow_power_portal" not in flow_step["data"]
        assert "flowpower_email" not in flow_step["data"]
        assert "flowpower_password" not in flow_step["data"]

        flow_portal = options_steps["flow_power_portal_options"]
        assert flow_portal["title"] == "Flow Power portal account"
        assert "separate from the tariff formula settings" in flow_portal["description"]
        assert "connect_portal" in flow_portal["data_description"]

        globird_step = options_steps["globird_options"]
        assert "Provider portal login page" in globird_step["description"]
        assert "configure_globird_portal" not in globird_step["data"]
        assert "globird_email" not in globird_step["data"]
        assert "globird_password" not in globird_step["data"]

        globird_portal = options_steps["globird_portal_options"]
        assert globird_portal["title"] == "GloBird portal account"
        assert "separate from the tariff and AEMO spike settings" in globird_portal[
            "description"
        ]
        assert "connect_globird_portal" in globird_portal["data_description"]


def test_flow_power_api_key_setup_validates_and_routes_sites():
    source = CONFIG_FLOW_PATH.read_text()
    validate_source = ast.get_source_segment(
        source,
        _top_level_function("validate_flow_power_api_key"),
    )
    setup_source = ast.get_source_segment(
        source,
        _config_flow_method("async_step_flow_power_setup"),
    )
    site_source = ast.get_source_segment(
        source,
        _config_flow_method("async_step_flow_power_site"),
    )

    assert validate_source is not None
    assert setup_source is not None
    assert site_source is not None
    assert "FLOW_POWER_KWATCH_REGIONS" in validate_source
    assert "client.dispatch5mins(api_region, period=60)" in validate_source
    assert "client.predispatch30mins(api_region, period=1)" in validate_source
    assert '"site_lookup_error": site_lookup_error or "no_sites"' in validate_source
    assert "CONF_FLOWPOWER_API_KEY" in setup_source
    assert "validate_flow_power_api_key" in setup_source
    assert 'user_input.get(CONF_FLOW_POWER_STATE, "NSW1")' in setup_source
    assert 'user_input[CONF_FLOW_POWER_PRICE_SOURCE] = "kwatch" if api_key else "aemo"' in setup_source
    assert "len(self._flow_power_sites) == 1" in setup_source
    assert "async_step_flow_power_site()" in setup_source
    assert "if self._flow_power_sites" in setup_source
    assert "CONF_FLOWPOWER_NMI" in site_source
    assert "_prefill_flow_power_network_tariff" in site_source


def test_flow_power_options_collects_kwatch_key_before_network_options():
    source = CONFIG_FLOW_PATH.read_text()
    options_source = ast.get_source_segment(
        source,
        _options_flow_method("async_step_flow_power_options"),
    )
    api_source = ast.get_source_segment(
        source,
        _options_flow_method("async_step_flow_power_api_key_options"),
    )
    site_source = ast.get_source_segment(
        source,
        _options_flow_method("async_step_flow_power_site_options"),
    )

    assert options_source is not None
    assert api_source is not None
    assert site_source is not None
    assert 'price_source == "kwatch"' in options_source
    assert "async_step_flow_power_api_key_options()" in options_source
    assert "validate_flow_power_api_key" in api_source
    assert "CONF_FLOWPOWER_API_KEY" in api_source
    assert 'self._get_option(CONF_FLOW_POWER_STATE, "NSW1")' in api_source
    assert "async_step_flow_power_site_options()" in api_source
    assert "async_step_flow_power_network_options()" in api_source
    assert "CONF_FLOWPOWER_NMI" in site_source


def test_flow_power_network_tariff_prefill_preserves_manual_selection():
    helper = ast.get_source_segment(
        CONFIG_FLOW_PATH.read_text(),
        _top_level_function("_prefill_flow_power_network_tariff"),
    )

    assert helper is not None
    assert "CONF_FLOWPOWER_NETWORK_TARIFF" in helper
    assert "flow_data.get(CONF_FP_NETWORK) or flow_data.get(CONF_FP_TARIFF_CODE)" in helper
    assert "get_tariff_codes_for_network" in helper


def test_provider_portal_login_errors_are_translated_for_setup_and_options():
    provider_error_keys = {
        "cannot_connect",
        "invalid_globird_auth",
        "captcha_required",
        "invalid_credentials",
        "invalid_mfa_code",
        "unknown",
    }

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_errors = data["config"]["error"]
        options_errors = data["options"]["error"]

        for key in provider_error_keys:
            assert config_errors.get(key), f"{path.name} missing config error {key}"
            assert options_errors.get(key), f"{path.name} missing options error {key}"


def test_globird_options_login_uses_shared_credential_validator():
    source = CONFIG_FLOW_PATH.read_text()
    helper = next(
        node
        for node in _module_tree().body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_validate_globird_credentials"
    )
    method = _options_flow_method("async_step_globird_portal_login_options")
    helper_source = ast.get_source_segment(source, helper)
    method_source = ast.get_source_segment(source, method)

    assert helper_source is not None
    assert "GloBirdClient" in helper_source
    assert method_source is not None
    assert "await _validate_globird_credentials(" in method_source
    assert "self._validate_globird_credentials" not in method_source


def test_optimization_options_exposes_enabled_toggle():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_OPTIMIZATION_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_ENABLED] = optimization_enabled" in method_source
    assert "CONF_OPTIMIZATION_AUTO_APPLY_RESERVE" in method_source
    assert (
        "new_options[CONF_OPTIMIZATION_AUTO_APPLY_RESERVE] = auto_apply_reserve_enabled"
        in method_source
    )
    assert "CONF_OPTIMIZATION_MANUAL_RESERVE" in method_source
    assert "CONF_OPTIMIZATION_EV_INTEGRATION" in method_source
    assert "new_options[CONF_OPTIMIZATION_EV_INTEGRATION] = ev_integration_enabled" in method_source
    assert "CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY" in method_source
    assert "planned_ev_load_entity = _normalize_optional_entity(" in method_source
    assert (
        "new_options[CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY] = planned_ev_load_entity"
        in method_source
    )
    assert "planned_ev_load_entity" in method_source
    assert "CONF_MONITORING_MODE" in method_source
    assert "new_options[CONF_MONITORING_MODE] = monitoring_mode" in method_source
    assert "battery_system == BATTERY_SYSTEM_SIGENERGY and monitoring_mode" in method_source
    assert "SERVICE_RESTORE_NORMAL" in method_source
    assert '{"source": "manual", "_native_control": True}' in method_source
    assert "CONF_HARDWARE_BACKUP_RESERVE" in method_source
    assert "new_options[CONF_HARDWARE_BACKUP_RESERVE] = hardware_backup_reserve" in method_source
    assert 'new_options.pop("_user_backup_reserve", None)' in method_source
    schema_source = method_source[method_source.index("schema_fields") :]
    assert (
        schema_source.index("CONF_OPTIMIZATION_ENABLED")
        < schema_source.index("CONF_OPTIMIZATION_AUTO_APPLY_RESERVE")
        < schema_source.index("CONF_OPTIMIZATION_EV_INTEGRATION")
        < schema_source.index("CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY")
        < schema_source.index("CONF_MONITORING_MODE")
    )
    assert (
        method_source.index("CONF_OPTIMIZATION_BACKUP_RESERVE")
        < method_source.index("CONF_HARDWARE_BACKUP_RESERVE")
        < method_source.index("CONF_OPTIMIZATION_BATTERY_CAPACITY_WH")
    )
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED] = spread_export_enabled" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "new_options[CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED] = spread_import_enabled" in method_source
    assert "CONF_OPTIMIZATION_DISABLE_IDLE" in method_source
    assert "new_options[CONF_OPTIMIZATION_DISABLE_IDLE] = disable_idle" in method_source
    assert "supports_no_idle_mode_provider(current_provider)" in method_source
    assert "if supports_no_idle_mode:" in method_source
    assert "CONF_PROFIT_MAX_ENABLED" in method_source
    assert "new_options[CONF_PROFIT_MAX_ENABLED] = profit_max_enabled" in method_source
    assert "new_options[CONF_CHARGE_BY_TIME_ENABLED] = charge_by_time_enabled" in method_source
    assert (
        method_source.index("CONF_PROFIT_MAX_ENABLED")
        < method_source.index("CONF_CHARGE_BY_TIME_ENABLED")
        < method_source.index("CONF_CHARGE_BY_TIME_TARGET_TIME")
    )
    assert "optimization_provider != OPT_PROVIDER_POWERSYNC" in method_source

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        step = json.loads(path.read_text())["options"]["step"]["optimization"]
        assert (
            step["data"]["optimization_auto_apply_reserve"]
            == "Auto-apply optimizer reserve"
        )
        assert "hardware backup reserve stays user controlled" in step[
            "data_description"
        ]["optimization_auto_apply_reserve"]
        assert step["data"]["optimization_disable_idle"] == "Disable idle mode"
        assert "supported TOU plans" in step["data_description"][
            "optimization_disable_idle"
        ]


def test_battery_init_options_do_not_mix_optimization_settings():
    source = CONFIG_FLOW_PATH.read_text()

    for method_name in (
        "async_step_init_tesla",
        "async_step_init_sigenergy",
        "async_step_init_sungrow",
        "async_step_init_foxess",
        "async_step_init_goodwe",
    ):
        method = _options_flow_method(method_name)
        method_source = ast.get_source_segment(source, method)

        assert method_source is not None
        assert "CONF_OPTIMIZATION_PROVIDER" not in method_source
        assert "CONF_OPTIMIZATION_BACKUP_RESERVE" not in method_source
        assert "CONF_OPTIMIZATION_MAX_GRID_IMPORT_W" not in method_source
        assert "CONF_OPTIMIZATION_MAX_GRID_EXPORT_W" not in method_source

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        options_steps = json.loads(path.read_text())["options"]["step"]
        for step_name in (
            "init_tesla",
            "init_sigenergy",
            "init_sungrow",
            "init_foxess",
            "init_goodwe",
        ):
            step = options_steps[step_name]

            assert "optimization_provider" not in step.get("data", {})
            assert "optimization_backup_reserve" not in step.get("data", {})
            assert "optimization_max_grid_import_w" not in step.get("data", {})
            assert "optimization_max_grid_export_w" not in step.get("data", {})
            assert "optimization_provider" not in step.get("data_description", {})
            assert "optimization_backup_reserve" not in step.get("data_description", {})
            assert "optimization_max_grid_import_w" not in step.get("data_description", {})
            assert "optimization_max_grid_export_w" not in step.get("data_description", {})
            assert "optimization" not in step["description"].lower()


def test_optimization_options_schedules_reload_after_flow_response():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    skip_reload_index = method_source.index('entry_data["_skip_reload"] = True')
    update_entry_index = method_source.index(
        "self.hass.config_entries.async_update_entry"
    )
    schedule_reload_index = method_source.index("self.hass.async_create_task")
    create_entry_index = method_source.index("return self.async_create_entry")

    assert skip_reload_index < update_entry_index
    assert update_entry_index < schedule_reload_index < create_entry_index
    assert (
        "self.hass.config_entries.async_reload(self.config_entry.entry_id)"
        in method_source
    )


def test_optimization_options_skip_reload_flag_is_gated_on_persisted_change():
    """OB-21: resubmitting the options flow with unchanged data/options must
    not set _skip_reload, or a later genuine structural change's update
    listener pops the stale flag and its reload is silently swallowed."""
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    persisted_changed_index = method_source.index("persisted_changed = (")
    skip_reload_index = method_source.index('entry_data["_skip_reload"] = True')
    update_entry_index = method_source.index(
        "self.hass.config_entries.async_update_entry"
    )

    assert persisted_changed_index < skip_reload_index < update_entry_index
    guard_source = method_source[persisted_changed_index:skip_reload_index]
    assert "new_data != dict(self.config_entry.data)" in guard_source
    assert "new_options != dict(self.config_entry.options)" in guard_source
    assert "and persisted_changed" in guard_source


def test_optimization_options_apply_tunables_in_place_without_reload():
    """Pure optimiser tunables apply live (no reload); structural keys reload."""
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    # Optimiser tunables are pushed into the running coordinator via the same
    # in-place path the mobile app uses, instead of a full reload.
    assert "await coordinator.set_settings(" in method_source
    assert "structural_change" in method_source
    # Structural keys still force a full reload. The auto-apply toggle is one of
    # them (it drives the reserve-transition logic), so it reloads rather than
    # applying live.
    assert "CONF_OPTIMIZATION_AUTO_APPLY_RESERVE" in method_source
    # EV integration toggle must reload — set_settings only flips the overlay
    # flag and never starts/stops the EV coordinator that schedules charging.
    assert "_opt_changed(CONF_OPTIMIZATION_EV_INTEGRATION" in method_source
    assert (
        "self.hass.config_entries.async_reload(self.config_entry.entry_id)"
        in method_source
    )
    update_entry_index = method_source.index(
        "self.hass.config_entries.async_update_entry"
    )
    set_settings_index = method_source.index("await coordinator.set_settings(")
    assert update_entry_index < set_settings_index
    assert "await coordinator._run_optimization()" not in method_source


def test_optimization_settings_api_exposes_planned_ev_load_entity():
    source = INIT_PATH.read_text()
    get_method = _init_class_method("OptimizationSettingsView", "get")
    post_method = _init_class_method("OptimizationSettingsView", "post")
    get_source = ast.get_source_segment(source, get_method)
    post_source = ast.get_source_segment(source, post_method)

    assert get_source is not None
    assert post_source is not None
    assert '"planned_ev_load_entity"' in get_source
    assert "CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY" in get_source
    assert "opt_coordinator._planned_ev_load_entity_id" in get_source
    assert '"planned_ev_load_entity"' in post_source
    assert "raw_entity.strip() if isinstance(raw_entity, str) else None" in post_source
    assert "new_data[CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY] = entity_id" in post_source
    assert "new_options[CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY] = entity_id" in post_source
    assert "entity_id or 'cleared'" in post_source
    assert '"max_grid_export_w"' in get_source
    assert "CONF_OPTIMIZATION_MAX_GRID_EXPORT_W" in get_source
    assert '"max_grid_export_w"' in post_source
    assert "Cleared max_grid_export_w" in post_source


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
    assert "CONF_OPTIMIZATION_AUTO_APPLY_RESERVE" in method_source
    assert "CONF_OPTIMIZATION_MANUAL_RESERVE" in method_source
    assert "CONF_OPTIMIZATION_EV_INTEGRATION" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_EV_INTEGRATION, False)" in method_source
    assert "CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY" in method_source
    assert "_normalize_optional_entity(" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY)" in method_source
    assert "CONF_MONITORING_MODE" in method_source
    assert "user_input.get(CONF_MONITORING_MODE, False)" in method_source
    assert "CONF_HARDWARE_BACKUP_RESERVE" in method_source
    schema_source = method_source[method_source.index("schema_fields") :]
    assert (
        schema_source.index("CONF_OPTIMIZATION_ENABLED")
        < schema_source.index("CONF_OPTIMIZATION_AUTO_APPLY_RESERVE")
        < schema_source.index("CONF_OPTIMIZATION_EV_INTEGRATION")
        < schema_source.index("CONF_OPTIMIZATION_PLANNED_EV_LOAD_ENTITY")
        < schema_source.index("CONF_MONITORING_MODE")
    )
    assert (
        method_source.index("CONF_OPTIMIZATION_BACKUP_RESERVE")
        < method_source.index("CONF_HARDWARE_BACKUP_RESERVE")
        < method_source.index("CONF_OPTIMIZATION_BATTERY_CAPACITY_WH")
    )
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in method_source
    assert "CONF_OPTIMIZATION_DISABLE_IDLE" in method_source
    assert "user_input.get(CONF_OPTIMIZATION_DISABLE_IDLE, False)" in method_source
    assert "supports_no_idle_mode_provider(" in method_source
    assert "if supports_no_idle_mode:" in method_source
    assert "CONF_PROFIT_MAX_ENABLED" in method_source
    assert "user_input.get(CONF_PROFIT_MAX_ENABLED, False)" in method_source
    assert "CONF_CHARGE_BY_TIME_ENABLED" in method_source
    assert "user_input.get(CONF_CHARGE_BY_TIME_ENABLED, False)" in method_source
    assert (
        method_source.index("CONF_PROFIT_MAX_ENABLED")
        < method_source.index("CONF_CHARGE_BY_TIME_ENABLED")
        < method_source.index("CONF_CHARGE_BY_TIME_TARGET_TIME")
    )

    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        step = json.loads(path.read_text())["config"]["step"]["ml_options"]
        assert (
            step["data"]["optimization_auto_apply_reserve"]
            == "Auto-apply optimizer reserve"
        )
        assert "hardware backup reserve stays user controlled" in step[
            "data_description"
        ]["optimization_auto_apply_reserve"]
        assert step["data"]["optimization_disable_idle"] == "Disable idle mode"
        assert "supported TOU plans" in step["data_description"][
            "optimization_disable_idle"
        ]


def test_initial_smart_optimization_saves_charge_by_time_aliases_to_ml_options():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_ml_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "data[CONF_PROFIT_MAX_TARGET_TIME]" not in method_source
    assert "data[CONF_PROFIT_MAX_TARGET_SOC]" not in method_source
    assert (
        "self._ml_options[CONF_PROFIT_MAX_TARGET_TIME] = self._ml_options[\n"
        "                    CONF_CHARGE_BY_TIME_TARGET_TIME"
    ) in method_source
    assert (
        "self._ml_options[CONF_PROFIT_MAX_TARGET_SOC] = self._ml_options[\n"
        "                    CONF_CHARGE_BY_TIME_TARGET_SOC"
    ) in method_source


def test_no_idle_option_is_provider_scoped():
    source = CONFIG_FLOW_PATH.read_text()
    initial_method = _config_flow_method("async_step_ml_options")
    initial_source = ast.get_source_segment(source, initial_method)
    options_method = _options_flow_method("async_step_optimization")
    options_source = ast.get_source_segment(source, options_method)

    assert initial_source is not None
    assert options_source is not None
    assert "supports_no_idle_mode_provider(" in initial_source
    assert "supports_no_idle_mode_provider(current_provider)" in options_source

    for method_source in (initial_source, options_source):
        assert "CONF_OPTIMIZATION_DISABLE_IDLE" in method_source
        assert "if supports_no_idle_mode:" in method_source
        assert "else False" in method_source


def test_powerwall_smart_optimization_hides_spread_options():
    source = CONFIG_FLOW_PATH.read_text()

    initial_method = _config_flow_method("async_step_ml_options")
    initial_source = ast.get_source_segment(source, initial_method)
    options_method = _options_flow_method("async_step_optimization")
    options_source = ast.get_source_segment(source, options_method)

    assert initial_source is not None
    assert options_source is not None
    assert "is_tesla = battery_system == BATTERY_SYSTEM_TESLA" in initial_source
    assert "is_tesla = battery_system == BATTERY_SYSTEM_TESLA" in options_source

    for method_source in (initial_source, options_source):
        assert "False\n                    if is_tesla" in method_source
        spread_block_start = method_source.index("if not is_tesla:")
        profit_block_start = method_source.index(
            "CONF_PROFIT_MAX_ENABLED",
            spread_block_start,
        )
        spread_schema_block = method_source[spread_block_start:profit_block_start]

        assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED" in spread_schema_block
        assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED" in spread_schema_block


def test_initial_setup_routes_to_combined_optimization_options_page():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_battery_system")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "return await self.async_step_ml_options()" in method_source
    assert "return await self.async_step_optimization_provider()" not in method_source


def test_options_menu_exposes_editable_battery_system_section():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_init")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert 'menu_options = ["pricing", "battery_system"]' in method_source
    assert "battery_system = self._effective_battery_system()" in method_source
    assert 'menu_options.append("alphaess_connection")' in method_source
    assert 'menu_options.append("anker_solix")' in method_source
    assert 'menu_options.append("custom_battery")' in method_source


def test_options_battery_system_selector_persists_and_routes_selection():
    source = CONFIG_FLOW_PATH.read_text()
    selector_method = _options_flow_method("async_step_battery_system")
    selector_source = ast.get_source_segment(source, selector_method)
    save_method = _options_flow_method("_save_battery_system_selection")
    save_source = ast.get_source_segment(source, save_method)
    route_method = _options_flow_method("_route_to_battery_options")
    route_source = ast.get_source_segment(source, route_method)

    assert selector_source is not None
    assert save_source is not None
    assert route_source is not None
    assert "BATTERY_SYSTEMS.items()" in selector_source
    assert "self._save_battery_system_selection(battery_system)" in selector_source
    assert "return await self._route_to_battery_options(battery_system)" in selector_source
    assert "new_data[CONF_BATTERY_SYSTEM] = battery_system" in save_source
    assert "new_options[CONF_BATTERY_SYSTEM] = battery_system" in save_source
    for target in (
        "async_step_tesla_connection",
        "async_step_sigenergy_connection",
        "async_step_sungrow_connection",
        "async_step_foxess_connection_options",
        "async_step_goodwe_connection_options",
        "async_step_alphaess_connection",
        "async_step_esy_sunhome_connection",
        "async_step_solax_battery_options",
        "async_step_saj_h2_connection",
        "async_step_fronius_reserva_connection",
        "async_step_neovolt_connection",
        "async_step_solaredge_connection",
        "async_step_anker_solix",
        "async_step_custom_battery",
    ):
        assert target in route_source


def test_custom_battery_options_persist_custom_system_and_monitoring_mode():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_custom_battery")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_CUSTOM" in method_source
    assert "CONF_OPTIMIZATION_PROVIDER: OPT_PROVIDER_POWERSYNC" in method_source
    assert "CONF_OPTIMIZATION_ENABLED: True" in method_source
    assert "CONF_MONITORING_MODE: True" in method_source
    assert "CONF_OPTIMIZATION_EV_INTEGRATION: False" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_EXPORT_ENABLED: False" in method_source
    assert "CONF_OPTIMIZATION_SPREAD_IMPORT_ENABLED: False" in method_source
    assert "return self._save_connection_and_reload(updates)" in method_source


def test_options_optimization_uses_effective_battery_system():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_optimization")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "battery_system = self._effective_battery_system()" in method_source
    assert "is_custom = battery_system == BATTERY_SYSTEM_CUSTOM" in method_source
    assert "if is_custom:\n                optimization_provider = OPT_PROVIDER_POWERSYNC" in method_source
    assert "if is_custom:\n                monitoring_mode = True" in method_source


def test_anker_and_alphaess_have_options_connection_pages():
    source = CONFIG_FLOW_PATH.read_text()
    anker_method = _options_flow_method("async_step_anker_solix")
    anker_source = ast.get_source_segment(source, anker_method)
    alphaess_method = _options_flow_method("async_step_alphaess_connection")
    alphaess_source = ast.get_source_segment(source, alphaess_method)

    assert anker_source is not None
    assert alphaess_source is not None
    assert "CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_ANKER_SOLIX" in anker_source
    assert "ANKER_SOLIX_CONNECTION_TYPES.items()" in anker_source
    assert "AnkerSolixX1ModbusController" in anker_source
    assert "AnkerSolixEntityController" in anker_source
    assert "return self._save_connection_and_reload(updates)" in anker_source
    assert "CONF_BATTERY_SYSTEM: BATTERY_SYSTEM_ALPHAESS" in alphaess_source
    assert "AlphaESSController" in alphaess_source
    assert "AlphaESSCloudClient" in alphaess_source
    assert "CONF_ALPHAESS_CLOUD_APP_SECRET" in alphaess_source
    assert "return self._save_connection_and_reload(updates, option_updates)" in alphaess_source


def test_battery_system_options_labels_are_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        menu_options = data["options"]["step"]["init"]["menu_options"]
        battery_step = data["options"]["step"]["battery_system"]
        custom_step = data["options"]["step"]["custom_battery"]
        anker_step = data["options"]["step"]["anker_solix"]

        assert menu_options["battery_system"] == "Battery / control method"
        assert menu_options["custom_battery"] == "Custom external controller"
        assert menu_options["anker_solix"] == "Anker Solix connection"
        assert menu_options["alphaess_connection"] == "AlphaESS connection"
        assert battery_step["data"]["battery_system"] == "Battery / control method"
        assert "primary battery" in battery_step["data_description"]["battery_system"]
        assert custom_step["data"]["custom_battery_level_entity"] == "Battery level sensor"
        assert custom_step["data"]["optimization_allow_grid_charge"] == "Allow grid charging"
        assert anker_step["data"]["anker_solix_connection_type"] == "Connection type"
        assert anker_step["data"]["anker_solix_max_discharge_kw"] == "Maximum discharge power"


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


def test_goodwe_runtime_auto_uses_entity_telemetry_for_tcp():
    init_source = (
        ROOT / "custom_components" / "power_sync" / "__init__.py"
    ).read_text()

    assert "_resolve_goodwe_entity_telemetry_prefix" in init_source
    assert "GoodWe TCP setup detected telemetry entity prefix" in init_source
    assert "entity_telemetry_prefix=goodwe_entity_telemetry_prefix" in init_source
    assert "goodwe_protocol == \"tcp\"" in init_source
    assert "DEFAULT_GOODWE_PORT_TCP" in init_source


def test_goodwe_connection_flow_accepts_tcp_entity_telemetry_before_direct_probe():
    source = CONFIG_FLOW_PATH.read_text()
    method = _config_flow_method("async_step_goodwe_connection")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    assert "resolve_goodwe_entity_telemetry_prefix" in method_source
    assert "if entity_telemetry_prefix" in method_source
    assert "else await test_goodwe_connection" in method_source


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
            "elif is_tesla:"
        )
    ]

    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in sungrow_branch
    assert "return await self.async_step_inverter_brand()" in sungrow_branch


def test_custom_tariff_export_rates_allow_negative_values():
    source = CONFIG_FLOW_PATH.read_text()
    custom_method = _options_flow_method("async_step_custom_tariff_options")
    period_method = _options_flow_method("async_step_tariff_period_options")
    custom_source = ast.get_source_segment(source, custom_method)
    period_source = ast.get_source_segment(source, period_method)

    assert custom_source is not None
    assert period_source is not None

    assert 'vol.Required("fit_rate", default=default_fit)' in custom_source
    assert "min=-100, max=100" in custom_source
    assert 'vol.Required("export_rate", default=5)' in period_source
    assert "min=-100, max=200" in period_source
    assert "Default export earnings" in STRINGS_PATH.read_text()
    assert "Use a negative value when you pay to export" in TRANSLATIONS_PATH.read_text()


def test_tesla_curtailment_options_expose_powerwall_offgrid_fallback():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_curtailment_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    tesla_branch = method_source[
        method_source.index("elif is_tesla:\n                # Tesla") : method_source.index(
            "# No AC inverter - route to weather options"
        )
    ]
    tesla_schema_branch = method_source[
        method_source.index("if is_tesla:\n            # Tesla Powerwall")
        : method_source.index("return self.async_show_form")
    ]

    assert "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT" in tesla_branch
    assert "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT" in tesla_schema_branch
    assert "CONF_AC_INVERTER_CURTAILMENT_ENABLED" in method_source
    assert "battery_system = self._get_option(" in method_source
    assert "is_tesla = battery_system == BATTERY_SYSTEM_TESLA" in method_source


def test_non_tesla_curtailment_options_do_not_expose_powerwall_controls():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_curtailment_options")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    non_tesla_submit_branch = method_source[
        method_source.index("else:\n                ac_enabled = user_input.get(")
        : method_source.index("# Build schema based on battery system")
    ]

    assert "CONF_POWERWALL_OFFGRID_AS_CURTAILMENT] = False" in non_tesla_submit_branch
    assert "battery_system = self._get_option(" in method_source
    assert "is_tesla = battery_system == BATTERY_SYSTEM_TESLA" in method_source
    assert "if is_tesla:\n            # Tesla Powerwall" in method_source
    assert "else:\n                ac_enabled = user_input.get(" in method_source
    assert "else:\n            # Tesla" not in method_source


def test_disabling_curtailment_restores_owned_inverter_limits():
    source = CONFIG_FLOW_PATH.read_text()
    restore_method = _options_flow_method("_restore_export_rule")
    restore_source = ast.get_source_segment(source, restore_method)
    helper_method = _options_flow_method("_restore_owned_curtailment_limits")
    helper_source = ast.get_source_segment(source, helper_method)

    assert restore_source is not None
    assert helper_source is not None
    assert "await self._restore_owned_curtailment_limits()" in restore_source
    assert "battery_system != BATTERY_SYSTEM_TESLA" in restore_source

    for state_key in (
        "sigenergy_curtailment_state",
        "alphaess_curtailment_state",
        "goodwe_curtailment_state",
        "foxess_curtailment_state",
        "solaredge_curtailment_state",
        "sungrow_curtailment_state",
        "inverter_last_state",
    ):
        assert state_key in helper_source

    assert "_last_sigenergy_curtailment_reapply" in helper_source
    assert "_last_goodwe_curtailment_reapply" in helper_source
    assert "restore_when_state_lost: bool = False" in helper_source
    assert "not was_curtailed and not restore_when_state_lost" in helper_source
    assert '"GoodWe",' in helper_source
    assert "restore_when_state_lost=True" in helper_source
    assert "_last_foxess_curtailment_reapply" in helper_source
    assert "sungrow_coord.set_export_limit(None)" in helper_source
    assert 'entry_data["sungrow_power_limit_w"] = None' in helper_source
    assert 'entry_data["inverter_power_limit_w"] = None' in helper_source


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


def test_sungrow_hybrid_model_cannot_share_battery_modbus_endpoint():
    source = CONFIG_FLOW_PATH.read_text()
    method = _options_flow_method("async_step_inverter_config")
    method_source = ast.get_source_segment(source, method)

    assert method_source is not None
    conflict_index = method_source.index('errors["base"] = "sungrow_modbus_conflict"')
    conflict_block = method_source[conflict_index - 350 : conflict_index + 80]

    assert "inverter_model = user_input.get(CONF_INVERTER_MODEL)" in method_source
    assert 'not str(inverter_model or "").lower().startswith("sh")' not in conflict_block
    assert "inverter_host == sungrow_host" in conflict_block
    assert "inverter_port == sungrow_port" in conflict_block
    assert "inverter_slave_id == sungrow_slave_id" in conflict_block


def test_sungrow_same_endpoint_ac_inverter_poller_is_skipped():
    source = SENSOR_PATH.read_text()

    assert "def _sungrow_ac_inverter_matches_battery" in source
    assert "if inverter_enabled and not _sungrow_ac_inverter_matches_battery(entry)" in source
    assert "Skipping AC inverter status poller" in source


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
            assert step["data"]["optimization_ev_integration"] == "PowerSync EV charger plans"
            assert "PowerSync-managed charger schedules" in step["data_description"][
                "optimization_ev_integration"
            ]
            assert "same charging demand" in step["data_description"][
                "optimization_ev_integration"
            ]
            assert (
                step["data"]["optimization_load_entity"]
                == "Historical load sensor"
            )
            assert "recorder-backed live household load sensor" in step[
                "data_description"
            ]["optimization_load_entity"]
            assert "no-EV load sensor" in step["data_description"][
                "optimization_load_entity"
            ]
            assert (
                step["data"]["optimization_planned_ev_load_entity"]
                == "Planned EV load forecast sensor"
            )
            assert "forecast-only EV demand" in step["data_description"][
                "optimization_planned_ev_load_entity"
            ]
            assert "always adds this sensor" in step["data_description"][
                "optimization_planned_ev_load_entity"
            ]
            assert step["data"]["monitoring_mode"] == "Monitoring mode"
            assert "Block battery and inverter control commands" in step["data_description"]["monitoring_mode"]
            assert step["data"]["hardware_backup_reserve"] == "Hardware backup reserve"
            assert "temporary hold or force-control modes" in step["data_description"]["hardware_backup_reserve"]
            assert step["data"]["optimization_max_grid_import_w"] == "Maximum grid import"
            assert "no site import cap" in step["data_description"]["optimization_max_grid_import_w"]
            assert step["data"]["optimization_max_grid_export_w"] == "Maximum grid export"
            assert "DNSP export cap" in step["data_description"]["optimization_max_grid_export_w"]
            keys = list(step["data"])
            assert keys.index("optimization_backup_reserve") < keys.index("hardware_backup_reserve")
            assert keys.index("optimization_max_discharge_w") < keys.index("optimization_max_grid_export_w")
            assert keys.index("optimization_max_grid_export_w") < keys.index("optimization_max_grid_import_w")
            assert step["data"]["optimization_spread_export_enabled"] == "Spread export across window"
            assert "spreads planned battery export" in step["data_description"]["optimization_spread_export_enabled"]
            assert step["data"]["optimization_spread_import_enabled"] == "Spread import across window"
            assert "spreads planned grid charging" in step["data_description"]["optimization_spread_import_enabled"]
            assert step["data"]["profit_max_enabled"] == "Enable Profit Max"
            assert "profitable export opportunities" in step["data_description"]["profit_max_enabled"]
            assert step["data"]["charge_by_time_enabled"] == "Enable Charge By Time"
            assert "configured target SOC" in step["data_description"]["charge_by_time_enabled"]
            assert step["data"]["charge_by_time_target_time"] == "Charge By Time target time"
            assert step["data"]["charge_by_time_target_soc"] == "Charge By Time target SOC"
            assert keys.index("optimization_enabled") < keys.index("optimization_ev_integration")
            assert keys.index("optimization_ev_integration") < keys.index("optimization_planned_ev_load_entity")
            assert keys.index("optimization_planned_ev_load_entity") < keys.index("monitoring_mode")
            assert keys.index("profit_max_enabled") < keys.index("charge_by_time_enabled")
            assert keys.index("charge_by_time_enabled") < keys.index("charge_by_time_target_time")


def test_globird_tariff_guidance_is_translated():
    for path in (STRINGS_PATH, TRANSLATIONS_PATH):
        data = json.loads(path.read_text())
        config_step = data["config"]["step"]["aemo_config"]
        options_step = data["options"]["step"]["globird_options"]

        assert "{threshold_hint}" in config_step["description"]
        assert options_step["title"] == "Globird / AEMO settings"
        assert "tariff source" in options_step["description"]
