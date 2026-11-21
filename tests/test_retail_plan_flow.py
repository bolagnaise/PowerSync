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


def test_allowance_step_builds_and_removes_the_export_quota(modules):
    flow = _setup_flow(modules)
    flow._tariff_offpeak_rate = 0.30
    flow._tariff_fit_rate = 0.02
    flow._tariff_periods = [{"name": "PEAK", "start": 17, "end": 21, "days": "all_days", "import_rate": 0.5, "export_rate": 0.04}]
    flow._custom_tariff_data = {"name": "Mine"}

    form = asyncio.run(flow.async_step_tariff_allowance())
    defaults = {str(field): field.default for field in form["data_schema"]}
    assert defaults["export_allowance_kwh"] == 0.0
    assert (defaults["export_allowance_start"], defaults["export_allowance_end"]) == ("16:00", "21:00")

    form = asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 0, "export_allowance_kwh": 10, "export_allowance_start": "17:00",
         "export_allowance_end": "21:00", "export_allowance_price": 4}))
    assert form["errors"] == {"base": "export_allowance_not_higher"}
    form = asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 0, "export_allowance_kwh": 10, "export_allowance_start": "17:00",
         "export_allowance_end": "17:00", "export_allowance_price": 12}))
    assert form["errors"] == {"base": "export_allowance_invalid"}
    assert "export_quota" not in flow._custom_tariff_data, "a rejected form saves nothing"

    result = asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 24, "allowance_start": "11:00", "allowance_end": "14:00", "allowance_price": 0,
         "export_allowance_kwh": 10, "export_allowance_start": "17:00", "export_allowance_end": "21:00",
         "export_allowance_price": 12}))
    assert result["step_id"] == "battery_system"
    # The premium is measured from the highest feed-in rate in the window.
    assert flow._custom_tariff_data["export_quota"] == {
        "enabled": True, "name": "Premium export allowance", "windows": [["17:00", "21:00"]],
        "daily_cap_kwh": 10.0, "base_price_c_per_kwh": 4.0, "bonus_price_c_per_kwh": 8.0,
    }
    assert flow._custom_tariff_data["import_quota"]["daily_cap_kwh"] == 24.0
    overview = flow._rp_allowance_placeholders()["period_info"]
    assert "Premium export allowance: first 10 kWh 17:00-21:00 earn 12.00c/kWh, resets daily." in overview

    form = asyncio.run(flow.async_step_tariff_allowance())
    defaults = {str(field): field.default for field in form["data_schema"]}
    assert defaults["export_allowance_kwh"] == 10.0 and defaults["export_allowance_price"] == 12.0

    asyncio.run(flow.async_step_tariff_allowance({"allowance_kwh": 24, "allowance_price": 0, "export_allowance_kwh": 0}))
    assert "export_quota" not in flow._custom_tariff_data
    assert "import_quota" in flow._custom_tariff_data


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

    flow._tariff_fit_rate = 0.05
    result = asyncio.run(flow.async_step_tariff_allowance_options(
        {"allowance_kwh": 0, "export_allowance_kwh": 6, "export_allowance_start": "16:00",
         "export_allowance_end": "20:00", "export_allowance_price": 15}))
    assert result["step_id"] == "demand_charge_options"
    assert flow.saved[-1]["export_quota"]["bonus_price_c_per_kwh"] == 10.0
    assert "import_quota" not in flow.saved[-1]


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
    assert tariff["export_quota"]["windows"] == [["17:00", "21:00"]]
    assert tariff["export_quota"]["bonus_price_c_per_kwh"] == 8.0
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
    assert "export_quota" not in tariff
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
    assert set(tariff) == {"name", "utility", "currency", "seasons", "energy_charges", "sell_tariff",
                           "power_sync_explicit_periods", "power_sync_standard_rates"}
    assert tariff["power_sync_standard_rates"] == {"import": 0.2, "export": 0.05}


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


# --- review fixes -----------------------------------------------------------


def test_allowance_ending_at_midnight_is_stored_in_the_runtime_spelling(modules):
    flow_helpers = modules.retail_plan_flow
    quota, error = flow_helpers.build_import_quota(
        limit_kwh=10, start="21:00", end="24:00", price_minor=0, periods=[], standard_import=0.3,
    )
    assert error is None and quota["windows"] == [["21:00", "00:00"]]
    quota_module = retail_plan_support.load_quota_modules()
    rule = quota_module.custom_tariff_import_quota_rule({"import_quota": quota}, default_timezone="Australia/Sydney")
    assert rule is not None, "the runtime accepts the saved window"
    from zoneinfo import ZoneInfo

    sydney = ZoneInfo("Australia/Sydney")
    assert rule.contains(datetime(2026, 11, 2, 23, 30, tzinfo=sydney))
    assert not rule.contains(datetime(2026, 11, 2, 20, 30, tzinfo=sydney))

    # The form shows the saved midnight end as 24:00 again.
    flow = _setup_flow(modules)
    flow._tariff_import_quota = quota
    defaults = {field: field.default for field in asyncio.run(flow.async_step_tariff_allowance())["data_schema"]}
    assert defaults["allowance_end"] == "24:00"

    imported = {"schedule": {"baseImportC": 30, "baseExportC": 5, "dailySupplyC": 0, "rates": [], "allowances": [
        {"direction": "import", "startMinute": 1260, "endMinute": 1440, "limitKwh": 5, "priceC": 0, "period": "day"}]}}
    draft = modules.aer_tariff_import.schedule_to_tariff_draft(imported, utility="x")
    assert draft["import_quota"]["windows"] == [["21:00", "00:00"]]


def test_allowance_discount_uses_only_rates_that_apply_in_the_window(modules):
    covered = [{"name": "PEAK", "start": 11, "end": 14, "days": "all_days", "import_rate": 0.3, "export_rate": None}]
    quota, error = modules.retail_plan_flow.build_import_quota(
        limit_kwh=10, start="11:00", end="14:00", price_minor=20, periods=covered, standard_import=0.1,
    )
    assert error is None, "the 10c standard rate never applies inside a fully covered window"
    assert (quota["base_price_c_per_kwh"], quota["bonus_price_c_per_kwh"]) == (30.0, 10.0)

    weekdays_only = [{**covered[0], "days": "weekdays"}]
    quota, _ = modules.retail_plan_flow.build_import_quota(
        limit_kwh=10, start="11:00", end="14:00", price_minor=0, periods=weekdays_only, standard_import=0.1,
    )
    assert quota["base_price_c_per_kwh"] == 10.0, "weekends fall back to the standard rate"

    seasonal = [{**covered[0], "season": "Summer"}]
    seasons = [{"name": "Summer", "from_month": 11, "to_month": 3}, {"name": "Winter", "from_month": 4, "to_month": 10}]
    quota, _ = modules.retail_plan_flow.build_import_quota(
        limit_kwh=10, start="11:00", end="14:00", price_minor=0, periods=seasonal, standard_import=0.4, seasons=seasons,
    )
    assert quota["base_price_c_per_kwh"] == 30.0


def test_allowance_rebuild_keeps_other_quota_settings(modules):
    flow = _setup_flow(modules)
    flow._retail_plan_retailer = "ergon"
    asyncio.run(flow.async_step_retail_plan_preset({"retail_plan_preset": "ergon_solar_sharer_12f_2026"}))
    flow._custom_tariff_data = {"name": "Ergon"}
    form = asyncio.run(flow.async_step_tariff_allowance())
    unchanged = {field: field.default for field in form["data_schema"]}
    asyncio.run(flow.async_step_tariff_allowance(unchanged))
    quota = flow._custom_tariff_data["import_quota"]
    assert quota["stop_grid_charging_at_quota"] is True
    assert quota["timezone_token"] == "Australia/Brisbane"
    assert quota["name"] == "Solar Sharer free allowance"
    assert (quota["windows"], quota["daily_cap_kwh"], quota["bonus_price_c_per_kwh"]) == ([["11:00", "14:00"]], 24.0, 8.101)


def test_intermediate_usage_tiers_are_reported(modules):
    imported = modules.aer_tariff_import.import_prd_electricity_plan({
        "planId": "TIER@EME",
        "electricityContract": {"tariffPeriod": [{"rateBlockUType": "singleRate", "singleRate": {
            "displayName": "Tiered", "rates": [
                {"volume": 10, "unitPrice": "0.10"}, {"volume": 10, "unitPrice": "0.20"}, {"unitPrice": "0.30"}]}}]},
    })
    assert any("Tiered: usage tiers between the first block" in text for text in imported["warnings"])
    two_tier = modules.aer_tariff_import.import_prd_electricity_plan({
        "planId": "TWO@EME",
        "electricityContract": {"tariffPeriod": [{"rateBlockUType": "singleRate", "singleRate": {
            "rates": [{"volume": 10, "unitPrice": "0.10"}, {"unitPrice": "0.30"}]}}]},
    })
    assert not any("usage tiers" in text for text in two_tier["warnings"])


def _flow_namespace(modules):
    namespace = retail_plan_support.config_flow_namespace(modules)
    retail_plan_support.config_flow_class(modules, namespace)
    namespace.update(
        CONF_GLOBIRD_PLAN="globird_plan",
        GLOBIRD_PLAN_FOUR4FREE_CUSTOM="four4free_custom",
        GLOBIRD_PLAN_NOT_ZEROHERO="not_zerohero",
        CONF_ELECTRICITY_PROVIDER="electricity_provider",
        CONF_DAILY_SUPPLY_CHARGE="daily_supply_charge",
        CONF_AGL_BATTERY_REWARDS_OFFPEAK_EXPORT_RATE="agl_battery_rewards_offpeak_export_rate",
        dt_util=SimpleNamespace(now=lambda: datetime(2026, 11, 2, 12, 0)),
    )
    return namespace


def test_flat_tariff_allowance_discounts_the_flat_rate(modules):
    namespace = _flow_namespace(modules)
    flow_class = namespace["PowerSyncConfigFlow"]
    setup_step = retail_plan_support.flow_method(modules, "PowerSyncConfigFlow", "async_step_custom_tariff", namespace)
    options_step = retail_plan_support.flow_method(
        modules, "PowerSyncOptionsFlow", "async_step_custom_tariff_options", namespace)

    class Setup(modules.retail_plan_steps.RetailPlanSetupMixin):
        _selected_electricity_provider = "other"
        _tariff_currency = "AUD"
        hass = None
        _build_tariff_from_periods = flow_class._build_tariff_from_periods

        def __init__(self):
            self._custom_tariff_data = {}

        def _selector_unit(self, kind="minor_rate"):
            return "c/kWh"

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        async def async_step_custom_tariff(self, user_input=None):
            return await setup_step(self, user_input)

        async def async_step_battery_system(self):
            return {"step_id": "battery_system"}

    flow = Setup()
    form = asyncio.run(flow.async_step_custom_tariff({"tariff_type": "flat", "flat_rate": 30, "offpeak_rate": 15, "fit_rate": 5}))
    assert form["step_id"] == "tariff_allowance"
    asyncio.run(flow.async_step_tariff_allowance(
        {"allowance_kwh": 10, "allowance_start": "11:00", "allowance_end": "14:00", "allowance_price": 0}))
    assert flow._custom_tariff_data["import_quota"]["bonus_price_c_per_kwh"] == 30.0

    saved = []

    class Options:
        _provider = "other"
        _amber_options: dict = {}
        hass = SimpleNamespace(data={})

        def _get_option(self, key, default=None):
            return default

        def _selector_unit(self, kind="minor_rate"):
            return "c/kWh"

        def _currency(self):
            return "AUD"

        def _build_tariff_from_periods_compat(self, periods):
            return {"periods": periods, "standard": self._tariff_offpeak_rate}

        async def _save_custom_tariff(self, tariff):
            saved.append(tariff)

        async def async_step_tariff_allowance_options(self):
            return {"step_id": "tariff_allowance_options"}

    options = Options()
    asyncio.run(options_step(options, {"tariff_type": "flat", "flat_rate": 28, "offpeak_rate": 15, "fit_rate": 5}))
    assert options._tariff_offpeak_rate == 0.28 and saved[-1]["standard"] == 0.28


def test_options_removal_keeps_a_row_held_for_editing(modules):
    namespace = _flow_namespace(modules)
    namespace.update(DEFAULT_AGL_BATTERY_REWARDS_OFFPEAK_EXPORT_RATE=3.0)
    step = retail_plan_support.flow_method(modules, "PowerSyncOptionsFlow", "async_step_tariff_period_options", namespace)
    rows = [{"name": "PEAK", "start": hour, "end": hour + 1, "days": "all_days", "import_rate": 0.5, "export_rate": None}
            for hour in (16, 17, 18)]

    class Options:
        _provider = "other"

        def __init__(self):
            self._tariff_periods = [dict(row) for row in rows]
            self.saved = []

        def _get_option(self, key, default=None):
            return default

        def _build_tariff_from_periods_compat(self, periods):
            return {"periods": [dict(period) for period in periods]}

        async def _save_custom_tariff(self, tariff):
            self.saved.append(tariff["periods"])

        async def async_step_tariff_period_options(self, user_input=None):
            return {"step_id": "tariff_period_options"}

    flow = Options()
    asyncio.run(step(flow, {"edit_period": "0"}))
    asyncio.run(step(flow, {"remove_period": "0"}))
    assert [row["start"] for row in flow.saved[-1]] == [18, 16], "the edited row is still saved"
    assert flow._tariff_period_editing["start"] == 16


def test_fully_covered_schedule_reopens_with_its_standard_rates(modules):
    namespace = _flow_namespace(modules)
    flow_class = namespace["PowerSyncConfigFlow"]
    step = retail_plan_support.flow_method(modules, "PowerSyncOptionsFlow", "async_step_custom_tariff_options", namespace)
    context = SimpleNamespace(_tariff_offpeak_rate=0.30, _tariff_fit_rate=0.04, _tariff_plan_name="Covered",
                              _selected_electricity_provider="other", _tariff_currency="AUD", hass=None)
    tariff = flow_class._build_tariff_from_periods(context, [
        {"name": "PEAK", "start": 0, "end": 12, "days": "all_days", "import_rate": 0.5, "export_rate": 0.1},
        {"name": "SHOULDER", "start": 12, "end": 24, "days": "all_days", "import_rate": None, "export_rate": None},
    ])
    assert "OFF_PEAK" not in tariff["energy_charges"]["All Year"] and "OFF_PEAK_AUTO" not in tariff["energy_charges"]["All Year"]

    class Store:
        def get_custom_tariff(self):
            return tariff

    class Options:
        _provider = "other"
        hass = SimpleNamespace(data={"power_sync": {"entry": {"automation_store": Store()}}})

        def _get_option(self, key, default=None):
            return default

        def _selector_unit(self, kind="minor_rate"):
            return "c/kWh"

        def async_show_form(self, **kwargs):
            return kwargs

    form = step(Options())
    defaults = {field: field.default for field in asyncio.run(form)["data_schema"]}
    assert defaults["offpeak_rate"] == 30.0 and defaults["fit_rate"] == 4.0


def test_export_quota_premium_resolves_every_season_slot_and_keeps_saved_keys(modules):
    flow_helpers = modules.retail_plan_flow
    seasons = [
        {"name": "Summer", "from_month": 11, "to_month": 3},
        {"name": "Winter", "from_month": 4, "to_month": 10},
    ]
    periods = [
        {"name": "PEAK", "start": 20, "end": 24, "days": "weekdays", "import_rate": 0.5,
         "export_rate": 0.07, "season": "Winter"},
        {"name": "PEAK", "start": 20, "end": 24, "days": "weekdays", "import_rate": 0.5,
         "export_rate": 0.03, "season": "Summer"},
    ]
    quota, error = flow_helpers.build_export_quota(
        limit_kwh=5, start="20:00", end="24:00", price_minor=15, periods=periods,
        standard_export=0.02, seasons=seasons,
        existing={"name": "Night premium", "timezone_token": "Australia/Brisbane", "enabled": False},
    )
    assert error is None
    assert quota == {
        "name": "Night premium", "timezone_token": "Australia/Brisbane", "enabled": True,
        "windows": [["20:00", "00:00"]], "daily_cap_kwh": 5.0,
        # The winter weekday rate is the highest in the window.
        "base_price_c_per_kwh": 7.0, "bonus_price_c_per_kwh": 8.0,
    }
    assert flow_helpers.build_export_quota(
        limit_kwh=5, start="20:00", end="24:00", price_minor=7, periods=periods,
        standard_export=0.02, seasons=seasons,
    ) == (None, "export_allowance_not_higher"), "not above the winter rate"
    assert flow_helpers.export_quota_effective_price(quota) == 15.0
