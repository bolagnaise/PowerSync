"""Retail plan selector steps, period table and allowance in the config flows."""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

import retail_plan_support

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "electricity_prd_seasonal_tou.json"
STRINGS = ROOT / "custom_components" / "power_sync" / "strings.json"
TRANSLATIONS = ROOT / "custom_components" / "power_sync" / "translations" / "en.json"


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, content_type=None):
        return self._body


class _Session:
    def __init__(self, plan_detail=None, list_status=200):
        self.plan_detail = plan_detail
        self.list_status = list_status
        self.urls: list[str] = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        if "/plans?" in url:
            plans = [
                {"planId": "SYN0000001MRE1@EME", "displayName": "Synthetic Seasonal Free Hours",
                 "customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY",
                 "effectiveFrom": "2026-10-01T00:00:00Z", "geography": {"distributors": ["Ausgrid"]}},
                {"planId": "OTHER@EME", "displayName": "Elsewhere", "customerType": "RESIDENTIAL",
                 "fuelType": "ELECTRICITY", "geography": {"distributors": ["Energex"]}},
            ]
            return _Response(self.list_status, {"data": {"plans": plans}, "meta": {"totalPages": 1}})
        return _Response(200, {"data": self.plan_detail})


@pytest.fixture
def modules():
    session = _Session(plan_detail=json.loads(FIXTURE.read_text()))
    loaded = retail_plan_support.load_modules(session)
    loaded.session = session
    return loaded


def _setup_flow(modules):
    class Flow(modules.retail_plan_steps.RetailPlanSetupMixin):
        hass = object()

        def __init__(self):
            self.calls: list[str] = []
            self._custom_tariff_data = {}

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def _selector_unit(self, kind="minor_rate"):
            return "$/day" if kind == "daily" else "c/kWh"

        async def async_step_custom_tariff(self):
            self.calls.append("custom_tariff")
            return {"type": "next", "step_id": "custom_tariff"}

        async def async_step_battery_system(self):
            self.calls.append("battery_system")
            return {"type": "next", "step_id": "battery_system"}

    return Flow()


def _options(form, key):
    for field, config in form["data_schema"].items():
        if field == key:
            return [option["value"] for option in config.options]
    raise AssertionError(key)


def test_setup_imports_published_plan_for_selected_network(modules):
    flow = _setup_flow(modules)

    async def run():
        form = await flow.async_step_retail_plan_retailer()
        assert form["step_id"] == "retail_plan_retailer"
        assert "radian" not in _options(form, "retail_plan_retailer"), "values are PowerSync ids, not slugs"
        assert "io_energy" in _options(form, "retail_plan_retailer")
        form = await flow.async_step_retail_plan_retailer({"retail_plan_retailer": "red_energy"})
        assert form["step_id"] == "retail_plan_network"
        assert "NSW1|Ausgrid" in _options(form, "retail_plan_network")
        form = await flow.async_step_retail_plan_network({"retail_plan_network": "NSW1|Nope"})
        assert form["errors"] == {"base": "network_required"}
        form = await flow.async_step_retail_plan_network({"retail_plan_network": "NSW1|Ausgrid"})
        assert form["step_id"] == "retail_plan_source"
        assert _options(form, "retail_plan_source") == ["published", "preset", "manual"]
        assert [value for value, _label in modules.retail_plan_flow.preset_options("Red Energy")] == [
            "flat_rate", "simple_tou", "solar_sponge", "ev_friendly"], "only generic presets for Red Energy"
        form = await flow.async_step_retail_plan_source({"retail_plan_source": "published"})
        assert form["step_id"] == "retail_plan_published"
        assert _options(form, "retail_plan_id") == ["SYN0000001MRE1@EME", "__manual__"], "filtered to Ausgrid"
        assert form["description_placeholders"]["count"] == "1"
        result = await flow.async_step_retail_plan_published({"retail_plan_id": "SYN0000001MRE1@EME"})
        assert result["step_id"] == "custom_tariff"

    asyncio.run(run())
    assert modules.session.urls[0].startswith("https://cdr.energymadeeasy.gov.au/red-energy/cds-au/v1/energy/plans?")
    assert modules.session.urls[-1].endswith("/plans/SYN0000001MRE1%40EME")
    assert flow._tariff_plan_name == "Synthetic Seasonal Free Hours"
    assert flow._tariff_offpeak_rate == 0.275 and flow._tariff_fit_rate == 0.01
    assert flow._tariff_daily_supply_charge == 1.65
    assert flow._tariff_utility == "Red Energy"
    assert flow._tariff_source["plan_id"] == "SYN0000001MRE1@EME"
    assert flow._tariff_source["network"] == "Ausgrid"
    assert len(flow._tariff_seasons) == 4 and flow._tariff_periods
    assert flow._tariff_import_quota["daily_cap_kwh"] == 24.0


def test_published_plan_errors_offer_manual_entry(modules):
    flow = _setup_flow(modules)
    modules.session.list_status = 503
    flow._retail_plan_retailer = "agl"
    flow._retail_plan_region, flow._retail_plan_network = "NSW1", "Ausgrid"

    async def run():
        form = await flow.async_step_retail_plan_published()
        assert form["errors"] == {"base": "prd_unavailable"}
        assert _options(form, "retail_plan_id") == ["__manual__"]
        assert await flow.async_step_retail_plan_published({"retail_plan_id": "INJECTED@EME"}) is not None
        result = await flow.async_step_retail_plan_published({"retail_plan_id": "__manual__"})
        assert result["step_id"] == "custom_tariff"

    asyncio.run(run())
    assert getattr(flow, "_tariff_draft", None) is None
    assert flow._tariff_utility == "AGL"


def test_unknown_plan_id_is_rejected_before_any_detail_request(modules):
    flow = _setup_flow(modules)
    flow._retail_plan_retailer = "red_energy"
    flow._retail_plan_region, flow._retail_plan_network = "NSW1", "Ausgrid"

    async def run():
        await flow.async_step_retail_plan_published()
        form = await flow.async_step_retail_plan_published({"retail_plan_id": "OTHER@EME"})
        assert form["errors"] == {"base": "prd_plan_unknown"}

    asyncio.run(run())
    assert not any("/plans/" in url for url in modules.session.urls)


def test_other_retailer_goes_straight_to_manual_entry(modules):
    flow = _setup_flow(modules)
    flow._tariff_draft = {"plan_name": "stale"}
    result = asyncio.run(flow.async_step_retail_plan_retailer({"retail_plan_retailer": "other"}))
    assert result["step_id"] == "custom_tariff"
    assert flow._tariff_draft is None


def test_dedicated_providers_are_pointed_out_and_presets_offered(modules):
    flow = _setup_flow(modules)
    flow._retail_plan_retailer = "globird"
    flow._retail_plan_network = "Ausgrid"
    form = asyncio.run(flow.async_step_retail_plan_source())
    assert "dedicated GloBird" in form["description_placeholders"]["provider_hint"]
    assert _options(form, "retail_plan_source") == ["published", "preset", "manual"]
    form = asyncio.run(flow.async_step_retail_plan_source({"retail_plan_source": "preset"}))
    presets = _options(form, "retail_plan_preset")
    assert presets[0] == "globird_tou" and "ev_friendly" in presets and "agl_solar_savers" not in presets


def test_ergon_preset_keeps_reviewed_allowance_and_supply(modules):
    flow = _setup_flow(modules)
    flow._retail_plan_retailer = "ergon"
    flow._retail_plan_region, flow._retail_plan_network = "QLD1", "Ergon"
    result = asyncio.run(flow.async_step_retail_plan_preset({"retail_plan_preset": "ergon_solar_sharer_12f_2026"}))
    assert result["step_id"] == "custom_tariff"
    assert flow._tariff_offpeak_rate == 0.26412
    assert flow._tariff_daily_supply_charge == 1.77853
    assert flow._tariff_import_quota["daily_cap_kwh"] == 24.0
    assert flow._tariff_source == {"kind": "preset", "template_id": "ergon_solar_sharer_12f_2026",
                                   "retailer": "ergon", "region": "QLD1", "network": "Ergon"}
    by_name = {(p["name"], p["start"], p["end"]): p for p in flow._tariff_periods}
    assert by_name[("PEAK", 16, 21)]["import_rate"] == 0.48902
    assert by_name[("SUPER_OFF_PEAK", 11, 14)]["import_rate"] == 0.08101
    assert ("OFF_PEAK", 21, 24) not in by_name, "periods at the standard rate are not repeated"


def test_allowance_step_builds_and_removes_the_import_quota(modules):
    flow = _setup_flow(modules)
    flow._tariff_offpeak_rate = 0.30
    flow._tariff_periods = [{"name": "PEAK", "start": 16, "end": 21, "days": "all_days", "import_rate": 0.5, "export_rate": None}]
    flow._custom_tariff_data = {"name": "Mine"}

    form = asyncio.run(flow.async_step_tariff_allowance())
    defaults = {field: field.default for field in form["data_schema"]}
    assert defaults["allowance_kwh"] == 0.0 and defaults["allowance_start"] == "11:00"
    assert "Standard import 30.00c/kWh" in form["description_placeholders"]["period_info"]

    form = asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 10, "allowance_start": "11:00", "allowance_end": "14:00", "allowance_price": 35}))
    assert form["errors"] == {"base": "allowance_not_cheaper"}

    result = asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 24, "allowance_start": "11:00", "allowance_end": "14:30", "allowance_price": 0}))
    assert result["step_id"] == "battery_system"
    assert flow._custom_tariff_data["import_quota"] == {
        "enabled": True, "name": "Free import allowance", "windows": [["11:00", "14:30"]],
        "daily_cap_kwh": 24.0, "base_price_c_per_kwh": 30.0, "bonus_price_c_per_kwh": 30.0,
    }
    asyncio.run(flow.async_step_tariff_allowance({"allowance_kwh": 0}))
    assert "import_quota" not in flow._custom_tariff_data


def test_options_keep_current_rates_or_choose_a_plan(modules):
    class Flow(modules.retail_plan_steps.RetailPlanOptionsMixin):
        hass = object()
        saved: list[dict] = []

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def _selector_unit(self, kind="minor_rate"):
            return "c/kWh"

        async def async_step_custom_tariff_options(self):
            return {"step_id": "custom_tariff_options"}

        async def async_step_demand_charge_options(self):
            return {"step_id": "demand_charge_options"}

        async def _save_custom_tariff(self, tariff):
            self.saved.append(dict(tariff))

    flow = Flow()
    form = asyncio.run(flow.async_step_retail_plan_options())
    assert form["step_id"] == "retail_plan_options"
    assert _options(form, "retail_plan_source") == ["edit", "choose"]
    flow._tariff_draft = {"plan_name": "stale"}
    assert asyncio.run(flow.async_step_retail_plan_options({"retail_plan_source": "edit"}))["step_id"] == "custom_tariff_options"
    assert flow._tariff_draft is None
    form = asyncio.run(flow.async_step_retail_plan_options({"retail_plan_source": "choose"}))
    assert form["step_id"] == "retail_plan_retailer_options"

    flow._pending_custom_tariff = {"name": "Mine", "import_quota": {"daily_cap_kwh": 5}}
    flow._tariff_offpeak_rate = 0.3
    result = asyncio.run(flow.async_step_tariff_allowance_options({"allowance_kwh": 0}))
    assert result["step_id"] == "demand_charge_options"
    assert flow.saved[-1] == {"name": "Mine"}


def _built_tariff(modules, draft):
    flow_class = retail_plan_support.config_flow_class(modules)
    context = SimpleNamespace(_selected_electricity_provider="other", _tariff_currency="AUD", hass=None)
    modules.retail_plan_flow.apply_draft(context, draft)
    return flow_class, context, flow_class._build_tariff_from_periods(context, context._tariff_periods)


def _runtime_prices(modules, tariff, when):
    time = modules.tariff_time
    season = time.find_season_for_month(tariff["seasons"], when.month)
    periods = tariff["seasons"][season]["tou_periods"]
    buy = tariff["energy_charges"][season]
    sell = tariff["sell_tariff"]["energy_charges"][season]
    name = time.find_matching_tou_period(periods, when, buy_rates=buy, sell_rates=sell)
    return round(buy[name], 6), round(sell[name], 6)


def test_imported_plan_builds_a_tariff_that_prices_like_the_cloud(modules):
    imported = modules.aer_tariff_import.import_prd_electricity_plan(json.loads(FIXTURE.read_text()))
    draft = modules.aer_tariff_import.schedule_to_tariff_draft(imported, utility="Synthetic", region="NSW1")
    flow_class, _context, tariff = _built_tariff(modules, draft)

    assert set(tariff["seasons"]) == {"Apr–May", "Jun–Aug", "Sep–Oct", "Nov–Mar"}
    assert tariff["daily_supply_charge"] == 1.65
    assert tariff["import_quota"]["windows"] == [["11:00", "14:00"]]
    assert tariff["power_sync_plan_source"]["plan_id"] == "SYN0000001MRE1@EME"
    assert tariff["utility"] == "Synthetic"
    # Wednesday 13 January, Saturday 10 April, Monday 12 April 2027, local time.
    assert _runtime_prices(modules, tariff, datetime(2027, 1, 13, 16, 0)) == (0.55, 0.01)
    assert _runtime_prices(modules, tariff, datetime(2027, 1, 13, 18, 0)) == (0.55, 0.04)
    assert _runtime_prices(modules, tariff, datetime(2027, 1, 13, 12, 0)) == (0.275, 0.01)
    assert _runtime_prices(modules, tariff, datetime(2027, 4, 10, 16, 0)) == (0.242, 0.01)
    assert _runtime_prices(modules, tariff, datetime(2027, 4, 10, 12, 0)) == (0.275, 0.01)
    assert _runtime_prices(modules, tariff, datetime(2027, 4, 12, 9, 0)) == (0.275, 0.01)
    assert _runtime_prices(modules, tariff, datetime(2027, 4, 12, 18, 0)) == (0.275, 0.04)

    # Reopening the options editor recovers exactly the imported rows.
    assert flow_class._custom_tariff_periods(tariff) == tariff["power_sync_explicit_periods"]


def test_builder_supports_half_hours_day_lists_and_standard_rate_blanks(modules):
    draft = {
        "plan_name": "Half hours", "utility": "x", "standard_import_rate": 0.3, "standard_export_rate": 0.05,
        "daily_supply_charge": None, "import_quota": None, "source": None,
        "seasons": [{"name": "All Year", "from_month": 1, "to_month": 12}],
        "periods": [
            {"name": "PEAK", "label": "Peak", "start": 15.5, "end": 20.5, "days": [1, 2, 3], "import_rate": 0.5, "export_rate": None},
            {"name": "OFF_PEAK", "start": 22, "end": 6.5, "days": "all_days", "import_rate": None, "export_rate": 0.0},
        ],
    }
    flow_class, _context, tariff = _built_tariff(modules, draft)
    periods = tariff["seasons"]["All Year"]["tou_periods"]
    assert periods["PEAK"] == [{"fromDayOfWeek": 1, "toDayOfWeek": 3, "fromHour": 15, "toHour": 20, "fromMinute": 30, "toMinute": 30}]
    assert tariff["energy_charges"]["All Year"]["PEAK"] == 0.5
    assert tariff["sell_tariff"]["energy_charges"]["All Year"]["PEAK"] == 0.05, "blank export is the standard rate"
    assert tariff["energy_charges"]["All Year"]["OFF_PEAK"] == 0.3, "blank import is the standard rate"
    generated = periods["OFF_PEAK_AUTO"]
    assert {"fromDayOfWeek": 1, "toDayOfWeek": 3, "fromHour": 6, "toHour": 15, "fromMinute": 30, "toMinute": 30} in generated
    assert {"fromDayOfWeek": 1, "toDayOfWeek": 3, "fromHour": 20, "toHour": 22, "fromMinute": 30, "toMinute": 0} in generated
    assert {"fromDayOfWeek": 4, "toDayOfWeek": 6, "fromHour": 6, "toHour": 22, "fromMinute": 30, "toMinute": 0} in generated
    assert "daily_supply_charge" not in tariff and "import_quota" not in tariff
    assert _runtime_prices(modules, tariff, datetime(2026, 11, 2, 15, 45)) == (0.5, 0.05)  # Monday
    assert _runtime_prices(modules, tariff, datetime(2026, 11, 2, 15, 15)) == (0.3, 0.05)
    assert _runtime_prices(modules, tariff, datetime(2026, 11, 5, 15, 45)) == (0.3, 0.05)  # Thursday
    assert _runtime_prices(modules, tariff, datetime(2026, 11, 5, 6, 15)) == (0.3, 0.0)
    assert flow_class._custom_tariff_periods(tariff) == tariff["power_sync_explicit_periods"]


def test_whole_hour_rows_build_without_minute_fields(modules):
    flow_class = retail_plan_support.config_flow_class(modules)
    context = SimpleNamespace(_tariff_offpeak_rate=0.2, _tariff_fit_rate=0.05, _tariff_plan_name="",
                              _selected_electricity_provider="other", _tariff_currency="AUD", hass=None)
    tariff = flow_class._build_tariff_from_periods(
        context, [{"name": "PEAK", "start": 16, "end": 21, "days": "weekdays", "import_rate": 0.4, "export_rate": 0.05}])
    for ranges in tariff["seasons"]["All Year"]["tou_periods"].values():
        for item in ranges:
            assert "fromMinute" not in item and "toMinute" not in item
    assert set(tariff) == {"name", "utility", "currency", "seasons", "energy_charges", "sell_tariff", "power_sync_explicit_periods"}


def test_period_editor_can_edit_a_row_and_continue_without_adding(modules):
    namespace = retail_plan_support.config_flow_namespace(modules)
    flow_class = retail_plan_support.config_flow_class(modules, namespace)
    namespace["dt_util"] = SimpleNamespace(now=lambda: datetime(2026, 11, 2, 12, 0))
    step = retail_plan_support.flow_method(modules, "PowerSyncConfigFlow", "async_step_tariff_period", namespace)

    class Flow:
        _selected_electricity_provider = "other"
        _tariff_offpeak_rate = 0.3
        _tariff_fit_rate = 0.05
        _tariff_seasons = [{"name": "All Year", "from_month": 1, "to_month": 12}]
        _pop_tariff_period = staticmethod(flow_class._pop_tariff_period)
        _tariff_period_from_input = staticmethod(flow_class._tariff_period_from_input)
        _tariff_period_schema = staticmethod(flow_class._tariff_period_schema)
        _tariff_season_options = staticmethod(flow_class._tariff_season_options)
        _build_tariff_from_periods = flow_class._build_tariff_from_periods

        def __init__(self):
            self._tariff_periods = [
                {"name": "PEAK", "label": "Peak", "start": 16, "end": 21, "days": "all_days", "import_rate": 0.5, "export_rate": None},
                {"name": "OFF_PEAK", "start": 0, "end": 6, "days": "all_days", "import_rate": 0.1, "export_rate": None},
            ]

        def _selector_unit(self, kind="minor_rate"):
            return "$/day" if kind == "daily" else "c/kWh"

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        async def async_step_tariff_period(self, user_input=None):
            return await step(self, user_input)

        async def async_step_tariff_allowance(self):
            return {"step_id": "tariff_allowance"}

    flow = Flow()
    form = asyncio.run(flow.async_step_tariff_period())
    defaults = {field: field.default for field in form["data_schema"]}
    assert defaults["next_action"] == "finish", "a loaded table continues without adding a row by default"
    assert "| 1 | Peak | Mon-Sun | 16:00-21:00 | 50.00c/kWh | standard |" in form["description_placeholders"]["period_info"]
    assert "Today's import price" in form["description_placeholders"]["period_info"]

    form = asyncio.run(flow.async_step_tariff_period({"edit_period": "0"}))
    defaults = {field: field.default for field in form["data_schema"]}
    suggested = {field: field.description.get("suggested_value") for field in form["data_schema"]}
    assert (defaults["period_label"], defaults["period_start"], defaults["period_end"]) == ("Peak", "16:00", "21:00")
    assert suggested["import_rate"] == 50.0 and suggested["export_rate"] is None
    assert defaults["next_action"] == "add_finish"
    assert len(flow._tariff_periods) == 1

    result = asyncio.run(flow.async_step_tariff_period({
        "period_label": "Peak", "period_type": "PEAK", "period_start": "16:30", "period_end": "21:00",
        "period_days": ["1", "2", "3", "4", "5"], "import_rate": 52, "next_action": "add_finish",
    }))
    assert result == {"step_id": "tariff_allowance"}
    assert flow._tariff_periods[-1] == {"name": "PEAK", "season": "All Year", "start": 16.5, "end": 21,
                                        "days": "weekdays", "import_rate": 0.52, "export_rate": None, "label": "Peak"}
    assert flow._custom_tariff_data["energy_charges"]["All Year"]["PEAK"] == 0.52

    flow = Flow()
    asyncio.run(flow.async_step_tariff_period({"edit_period": "1"}))
    result = asyncio.run(flow.async_step_tariff_period({"next_action": "finish"}))
    assert result == {"step_id": "tariff_allowance"}
    assert len(flow._tariff_periods) == 2, "finishing during an edit keeps the row"


def test_every_new_step_field_and_error_has_strings():
    steps_source = (ROOT / "custom_components" / "power_sync" / "retail_plan_steps.py").read_text()
    flow_source = (ROOT / "custom_components" / "power_sync" / "config_flow.py").read_text()
    constants = dict(re.findall(r'^(CONF_[A-Z_]+) = "([a-z_]+)"', steps_source, re.M))
    helper_source = (ROOT / "custom_components" / "power_sync" / "retail_plan_flow.py").read_text()
    errors = set(re.findall(r'errors\["base"\] = "([a-z_]+)"', steps_source))
    errors |= set(re.findall(r'return None, "([a-z_]+)"', helper_source))
    errors |= {code for code in re.findall(r'return None, "([a-z_]+)"', flow_source) if code.startswith("period_")}
    assert {"prd_unavailable", "allowance_not_cheaper", "period_time_whole_hours"} <= errors
    config_steps = {
        "retail_plan_retailer": {constants["CONF_RETAIL_PLAN_RETAILER"]},
        "retail_plan_network": {constants["CONF_RETAIL_PLAN_NETWORK"]},
        "retail_plan_source": {constants["CONF_RETAIL_PLAN_SOURCE"]},
        "retail_plan_published": {constants["CONF_RETAIL_PLAN_ID"]},
        "retail_plan_preset": {constants["CONF_RETAIL_PLAN_PRESET"]},
        "tariff_allowance": {constants[name] for name in constants if name.startswith("CONF_ALLOWANCE")},
        "tariff_period": {"edit_period", "remove_period", "season", "period_label", "period_type", "period_start",
                          "period_end", "period_days", "import_rate", "export_rate", "next_action"},
        "custom_tariff": {"plan_name", "tariff_type", "flat_rate", "offpeak_rate", "fit_rate", "seasonal_rates",
                          "skip_tariff", "daily_supply_charge"},
    }
    for path in (STRINGS, TRANSLATIONS):
        content = json.loads(path.read_text())
        for scope, suffix in (("config", ""), ("options", "_options")):
            steps = content[scope]["step"]
            for step, fields in config_steps.items():
                name = step + suffix
                if scope == "options":
                    fields = fields - {"skip_tariff"}
                assert name in steps, (path.name, name)
                assert fields <= set(steps[name].get("data", {})), (path.name, name, fields - set(steps[name]["data"]))
            assert "add_another" not in steps["tariff_period" + suffix]["data"]
            missing = errors - set(content[scope]["error"])
            assert not missing, (path.name, scope, missing)
        assert "retail_plan_options" in content["options"]["step"]
        for scope in ("config", "options"):
            for name, step in content[scope]["step"].items():
                if name.startswith(("retail_plan", "tariff_allowance")):
                    for placeholder in re.findall(r"\{(\w+)\}", step.get("description", "")):
                        assert placeholder in steps_source, (name, placeholder)
