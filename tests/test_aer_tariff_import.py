"""Published AER plan import, retailer directory and network matching.

Ported from the PowerSync Cloud schedule checks so both importers read the
same synthetic Product Reference Data record identically.
"""

from __future__ import annotations

from datetime import datetime
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"
FIXTURE = ROOT / "tests" / "fixtures" / "electricity_prd_seasonal_tou.json"
TEST_PACKAGE = "powersync_aer_import_testpkg"
package = types.ModuleType(TEST_PACKAGE)
package.__path__ = [str(COMPONENT_ROOT)]
sys.modules[TEST_PACKAGE] = package

for module_name in ("aer_plans", "aer_tariff_import", "tariff_time", "quota", "tariff_quota"):
    qualified = f"{TEST_PACKAGE}.{module_name}"
    spec = importlib.util.spec_from_file_location(qualified, COMPONENT_ROOT / f"{module_name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)

aer_plans = sys.modules[f"{TEST_PACKAGE}.aer_plans"]
importer = sys.modules[f"{TEST_PACKAGE}.aer_tariff_import"]
tariff_time = sys.modules[f"{TEST_PACKAGE}.tariff_time"]
tariff_quota = sys.modules[f"{TEST_PACKAGE}.tariff_quota"]


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _rule_tuple(rule: dict) -> list:
    return [
        rule["direction"],
        rule["startMinute"],
        rule["endMinute"],
        rule["priceC"],
        rule.get("days"),
        rule.get("months"),
    ]


def test_public_record_becomes_gst_inclusive_half_hour_schedule():
    imported = importer.import_prd_electricity_plan(_fixture())

    assert imported["sourcePlanId"] == "SYN0000001MRE1@EME"
    assert imported["effectiveFrom"] == "2026-10-01"
    assert imported["clock"] == "local"
    schedule = imported["schedule"]
    assert schedule["dailySupplyC"] == 165, "supply converted from ex-GST dollars"
    assert schedule["baseImportC"] == 27.5, "longest-covering rate is the standard import"
    assert schedule["baseExportC"] == 1, "feed-in is GST-free"
    assert [_rule_tuple(rule) for rule in schedule["rates"]] == [
        ["import", 900, 1260, 55, None, [1, 2, 3, 6, 7, 8, 11, 12]],
        ["import", 840, 660, 24.2, [0, 6], [4, 5, 9, 10]],
        ["export", 1020, 1260, 4, None, None],
    ], "seasonal peaks merge, inclusive ends round up, rates equal to standard drop"
    assert [
        [rule["direction"], rule["startMinute"], rule["endMinute"], rule["limitKwh"], rule["priceC"], rule.get("months")]
        for rule in schedule["allowances"]
    ] == [
        ["import", 660, 840, 24, 0, None],
        ["export", 1020, 1260, 10, 12, None],
    ], "the same free allowance in every season is one year-round allowance"
    warnings = imported["warnings"]
    assert any("Legacy premium scheme" in text for text in warnings), "closed premium schemes are skipped"
    assert any("Controlled-load" in text for text in warnings)
    assert any("Three free hours" in text for text in warnings)


def test_missing_contract_and_usage_rates_are_rejected():
    with pytest.raises(importer.PrdImportError, match="prd_electricity_contract_missing"):
        importer.import_prd_electricity_plan({"planId": "X@EME"})
    with pytest.raises(importer.PrdImportError, match="prd_usage_rates_missing"):
        importer.import_prd_electricity_plan(
            {"planId": "X@EME", "electricityContract": {"tariffPeriod": [{"rateBlockUType": "singleRate"}]}}
        )


def test_unpublished_export_hours_use_lowest_feed_in_not_premium_window():
    partial = importer.import_prd_electricity_plan(
        {
            "planId": "SYN2@EME",
            "electricityContract": {
                "tariffPeriod": [
                    {"rateBlockUType": "singleRate", "dailySupplyCharge": "1", "singleRate": {"rates": [{"unitPrice": "0.3"}]}}
                ],
                "solarFeedInTariff": [
                    {
                        "tariffUType": "timeVaryingTariffs",
                        "timeVaryingTariffs": [
                            {"rates": [{"unitPrice": "0.08"}], "timeVariations": [{"startTime": "16:00", "endTime": "22:59"}]},
                            {"rates": [{"unitPrice": "0.00"}], "timeVariations": [{"startTime": "23:00", "endTime": "23:59"}]},
                        ],
                    }
                ],
            },
        }
    )
    assert partial["schedule"]["baseExportC"] == 0
    assert partial["schedule"]["rates"] == [
        {"direction": "export", "startMinute": 960, "endMinute": 1380, "priceC": 8, "label": "Feed-in"}
    ]
    assert partial["schedule"]["dailySupplyC"] == 110
    assert any("use 0c/kWh" in text for text in partial["warnings"])


def test_off_boundary_times_round_and_aest_plans_are_flagged():
    imported = importer.import_prd_electricity_plan(
        {
            "planId": "SYN3@EME",
            "electricityContract": {
                "timeZone": "AEST",
                "tariffPeriod": [
                    {
                        "rateBlockUType": "timeOfUseRates",
                        "timeOfUseRates": [
                            {"displayName": "Peak", "rates": [{"unitPrice": "0.40"}], "timeOfUse": [{"startTime": "16:10", "endTime": "20:59"}]},
                            {"displayName": "Off Peak", "rates": [{"unitPrice": "0.20"}], "timeOfUse": [{"startTime": "21:00", "endTime": "16:09"}]},
                        ],
                    }
                ],
            },
        }
    )
    assert imported["clock"] == "aest"
    assert [_rule_tuple(rule) for rule in imported["schedule"]["rates"]] == [
        ["import", 960, 1260, 44, None, None]
    ]
    assert any("half-hour" in text for text in imported["warnings"])

    draft = importer.schedule_to_tariff_draft(imported, utility="Ergon Energy", region="NSW1")
    assert any("AEST" in text for text in draft["warnings"])
    assert importer.schedule_to_tariff_draft(imported, utility="x", region="QLD1")["warnings"] == draft["warnings"][:-1]


def _price_at(draft: dict, when: datetime) -> tuple[float, float]:
    """Resolve a draft through the same rules the custom tariff builder uses."""
    season = tariff_time.find_season_for_month(
        {season["name"]: {"fromMonth": season["from_month"], "toMonth": season["to_month"]} for season in draft["seasons"]},
        when.month,
    )
    tesla_day = tariff_time.tesla_day_of_week(when)
    minute = when.hour * 60 + when.minute
    for period in draft["periods"]:
        if period.get("season", season) != season:
            continue
        days = {"weekdays": [1, 2, 3, 4, 5], "weekends": [0, 6], "all_days": list(range(7))}.get(period["days"], period["days"])
        start, end = int(period["start"] * 60), int(period["end"] * 60)
        inside = start <= minute < end if start < end else minute >= start or minute < end
        if inside and tesla_day in days:
            import_rate = period["import_rate"]
            export_rate = period["export_rate"]
            return (
                draft["standard_import_rate"] if import_rate is None else import_rate,
                draft["standard_export_rate"] if export_rate is None else export_rate,
            )
    return draft["standard_import_rate"], draft["standard_export_rate"]


def test_draft_maps_months_to_seasons_and_keeps_cloud_prices():
    imported = importer.import_prd_electricity_plan(_fixture())
    draft = importer.schedule_to_tariff_draft(imported, utility="Synthetic", region="NSW1", network="Ausgrid", retailer="other")

    assert draft["standard_import_rate"] == 0.275
    assert draft["standard_export_rate"] == 0.01
    assert draft["daily_supply_charge"] == 1.65
    assert [(s["name"], s["from_month"], s["to_month"]) for s in draft["seasons"]] == [
        ("Apr–May", 4, 5), ("Jun–Aug", 6, 8), ("Sep–Oct", 9, 10), ("Nov–Mar", 11, 3),
    ], "summer and winter peaks share a rate but HA seasons are contiguous month ranges"
    for period in draft["periods"]:
        assert period["season"] in {season["name"] for season in draft["seasons"]}
        assert (period["start"] * 2) == int(period["start"] * 2) and (period["end"] * 2) == int(period["end"] * 2)

    # Same evaluation points as the Cloud contract checks.
    assert _price_at(draft, datetime(2027, 1, 13, 16, 0)) == (0.55, 0.01)
    assert _price_at(draft, datetime(2027, 4, 10, 16, 0)) == (0.242, 0.01)
    assert _price_at(draft, datetime(2027, 1, 13, 18, 0)) == (0.55, 0.04), "overlapping import and export windows split"
    assert _price_at(draft, datetime(2027, 4, 12, 18, 0)) == (0.275, 0.04)
    assert _price_at(draft, datetime(2027, 4, 12, 9, 0)) == (0.275, 0.01), "Monday morning is not the weekend rate"
    assert _price_at(draft, datetime(2027, 4, 11, 9, 0)) == (0.242, 0.01)
    assert _price_at(draft, datetime(2027, 7, 1, 12, 0)) == (0.275, 0.01)

    assert draft["import_quota"] == {
        "enabled": True,
        "name": "Free Hours",
        "windows": [["11:00", "14:00"]],
        "daily_cap_kwh": 24.0,
        "base_price_c_per_kwh": 27.5,
        "bonus_price_c_per_kwh": 27.5,
    }
    rule = tariff_quota.custom_tariff_import_quota_rule(draft, default_timezone="Australia/Sydney")
    assert rule is not None and rule.effective_price_c_per_kwh(True) == 0.0
    # 12c for the first 10 kWh exported 17:00-21:00, then the 4c window rate.
    assert draft["export_quota"] == {
        "enabled": True,
        "name": "First 10 kWh evening",
        "windows": [["17:00", "21:00"]],
        "daily_cap_kwh": 10.0,
        "base_price_c_per_kwh": 4.0,
        "bonus_price_c_per_kwh": 8.0,
    }
    export_rule = tariff_quota.custom_tariff_export_quota_rule(draft, default_timezone="Australia/Sydney")
    assert export_rule is not None and export_rule.effective_price_c_per_kwh(True) == 12.0
    assert not any("premium export allowance" in text for text in draft["warnings"]), "export allowance is applied, not warned about"
    assert draft["source"] == {
        "kind": "aer_prd", "plan_id": "SYN0000001MRE1@EME", "effective_from": "2026-10-01",
        "retailer": "other", "region": "NSW1", "network": "Ausgrid",
    }


def test_draft_for_single_rate_plan_is_one_all_year_season_without_periods():
    imported = importer.import_prd_electricity_plan(
        {
            "planId": "SYN4@EME",
            "displayName": "Flat",
            "electricityContract": {
                "tariffPeriod": [{"rateBlockUType": "singleRate", "dailySupplyCharge": "1.2", "singleRate": {"rates": [{"unitPrice": "0.3"}]}}],
                "solarFeedInTariff": [{"tariffUType": "singleTariff", "singleTariff": {"rates": [{"unitPrice": "0.05"}]}}],
            },
        }
    )
    draft = importer.schedule_to_tariff_draft(imported, utility="x")
    assert draft["seasons"] == [{"name": "All Year", "from_month": 1, "to_month": 12}]
    assert draft["periods"] == []
    assert (draft["standard_import_rate"], draft["standard_export_rate"]) == (0.33, 0.05)
    assert draft["import_quota"] is None


def test_overnight_all_week_windows_stay_one_period_and_types_follow_price():
    imported = {
        "label": "EV",
        "schedule": {
            "baseImportC": 30, "baseExportC": 5, "dailySupplyC": 100,
            "rates": [
                {"direction": "import", "startMinute": 1320, "endMinute": 360, "priceC": 8, "label": "Overnight"},
                {"direction": "import", "startMinute": 960, "endMinute": 1260, "priceC": 50, "label": "Peak"},
                {"direction": "import", "startMinute": 420, "endMinute": 600, "priceC": 40, "label": "Shoulder"},
            ],
            "allowances": [],
        },
    }
    draft = importer.schedule_to_tariff_draft(imported, utility="x")
    assert [(p["name"], p["start"], p["end"], p["days"], p["import_rate"]) for p in draft["periods"]] == [
        ("SHOULDER", 7, 10, "all_days", 0.4),
        ("PEAK", 16, 21, "all_days", 0.5),
        ("OFF_PEAK", 22, 6, "all_days", 0.08),
    ]


def test_allowance_not_cheaper_or_with_varying_rate_is_handled_conservatively():
    base = {"baseImportC": 30, "baseExportC": 5, "dailySupplyC": 0}
    varying = importer.schedule_to_tariff_draft(
        {"schedule": {**base,
                      "rates": [{"direction": "import", "startMinute": 720, "endMinute": 780, "priceC": 20, "label": "Dip"}],
                      "allowances": [{"direction": "import", "startMinute": 660, "endMinute": 840, "limitKwh": 10, "priceC": 0, "period": "day", "label": "Free"}]}},
        utility="x",
    )
    assert varying["import_quota"]["bonus_price_c_per_kwh"] == 20, "discount uses the lowest underlying rate"
    assert any("varies inside" in text for text in varying["warnings"])

    dearer = importer.schedule_to_tariff_draft(
        {"schedule": {**base, "rates": [],
                      "allowances": [{"direction": "import", "startMinute": 660, "endMinute": 840, "limitKwh": 10, "priceC": 35, "period": "day"}]}},
        utility="x",
    )
    assert dearer["import_quota"] is None

    seasonal = importer.schedule_to_tariff_draft(
        {"schedule": {**base, "rates": [],
                      "allowances": [{"direction": "import", "startMinute": 660, "endMinute": 840, "limitKwh": 10, "priceC": 0, "period": "day", "months": [1, 2]}]}},
        utility="x",
    )
    assert seasonal["import_quota"] is None and any("seasonal allowance" in text for text in seasonal["warnings"])


def test_export_allowance_premium_is_measured_from_the_highest_window_rate():
    base = {"baseImportC": 30, "baseExportC": 5, "dailySupplyC": 0}
    varying = importer.schedule_to_tariff_draft(
        {"schedule": {**base,
                      "rates": [{"direction": "export", "startMinute": 1080, "endMinute": 1140, "priceC": 9, "label": "Evening"}],
                      "allowances": [{"direction": "export", "startMinute": 1020, "endMinute": 1260, "limitKwh": 8, "priceC": 15, "period": "day", "label": "Premium"}]}},
        utility="x",
    )
    assert varying["import_quota"] is None
    assert varying["export_quota"]["base_price_c_per_kwh"] == 9
    assert varying["export_quota"]["bonus_price_c_per_kwh"] == 6, "premium never exceeds the real one"
    assert any("premium uses the highest rate" in text for text in varying["warnings"])

    lower = importer.schedule_to_tariff_draft(
        {"schedule": {**base, "rates": [],
                      "allowances": [{"direction": "export", "startMinute": 1020, "endMinute": 1260, "limitKwh": 8, "priceC": 5, "period": "day", "label": "Flat"}]}},
        utility="x",
    )
    assert lower["export_quota"] is None
    assert any("pays no more than the usual feed-in rate" in text for text in lower["warnings"])

    seasonal = importer.schedule_to_tariff_draft(
        {"schedule": {**base, "rates": [],
                      "allowances": [{"direction": "export", "startMinute": 1020, "endMinute": 1260, "limitKwh": 8, "priceC": 15, "period": "day", "months": [12]}]}},
        utility="x",
    )
    assert seasonal["export_quota"] is None and any("seasonal allowance" in text for text in seasonal["warnings"])


def test_provider_directory_and_network_matching():
    ids = [entry.id for entry in aer_plans.ELECTRICITY_PROVIDER_DIRECTORY]
    assert len(set(ids)) == len(ids)
    slugs = {entry.prd for entry in aer_plans.ELECTRICITY_PROVIDER_DIRECTORY if entry.prd}
    assert {"agl", "origin", "energyaustralia", "red-energy", "alinta", "engie", "powershop", "ovo-energy",
            "globird", "covau", "flow-power", "amber", "energy-locals", "momentum", "lumo", "dodo", "tango",
            "sumo-power", "1st-energy", "nectr", "diamond", "actewagl", "radian", "kogan", "ergon", "blue-nrg",
            "aurora", "arc-energy"} <= slugs
    assert aer_plans.retailer_entry("io_energy").prd == "radian"
    assert aer_plans.retailer_entry("other").prd is None

    assert aer_plans.prd_network_matches("AusNet Services (electricity)", "AusNet")
    assert aer_plans.prd_network_matches("Citipower", "CitiPower")
    assert aer_plans.prd_network_matches("Essential Energy Far West", "Essential Energy")
    assert aer_plans.prd_network_matches("SA Power Networks", "SA Power Networks")
    assert aer_plans.prd_network_matches("Endeavour", "Endeavour Energy")
    assert not aer_plans.prd_network_matches("Energex", "Ergon")
    for networks in aer_plans.ELECTRICITY_NETWORKS.values():
        for network in networks:
            assert aer_plans.prd_network_matches(network, network)

    assert aer_plans.parse_network_option("VIC1|CitiPower") == ("VIC1", "CitiPower")
    assert aer_plans.parse_network_option("VIC1|Ausgrid") is None
    assert aer_plans.parse_network_option("bad") is None


def test_public_plans_filter_by_network_and_sort_market_offers_newest_first():
    summarised = aer_plans.summarise_prd_plans([
        {"planId": "B@EME", "displayName": "Saver", "customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY",
         "effectiveFrom": "2026-10-01T00:00:00Z", "geography": {"distributors": ["Ausgrid"]}},
        {"planId": "S@EME", "displayName": "Basic", "customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY",
         "type": "STANDING", "geography": {"distributors": ["Ausgrid"]}},
        {"planId": "E@EME", "displayName": "Other", "customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY",
         "geography": {"distributors": ["Endeavour"]}},
        {"planId": "A@EME", "displayName": "Saver", "customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY",
         "effectiveFrom": "2026-11-01T00:00:00Z", "geography": {"distributors": ["Ausgrid"]}},
        {"planId": "C@EME", "displayName": "Business", "customerType": "BUSINESS", "fuelType": "ELECTRICITY",
         "geography": {"distributors": ["Ausgrid"]}},
        {"planId": "G@EME", "displayName": "Gas", "customerType": "RESIDENTIAL", "fuelType": "GAS",
         "geography": {"distributors": ["Ausgrid"]}},
    ])
    plans = aer_plans.public_plans_for_network(summarised, "Ausgrid")
    assert [plan["planId"] for plan in plans] == ["A@EME", "B@EME", "S@EME"]
    assert plans[0]["effectiveFrom"] == "2026-11-01"
    assert plans[2]["standing"] is True
    assert [plan["planId"] for plan in aer_plans.public_plans_for_network(summarised, "Endeavour Energy")] == ["E@EME"]
    assert not aer_plans.valid_prd_plan_id("../x@EME")


class _Response:
    def __init__(self, status: int, body: object):
        self.status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, content_type=None):
        return self._body


class _Session:
    def __init__(self, responses: list[_Response]):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_prd_fetch_pages_uses_versions_and_checks_identity():
    import asyncio

    page = {"customerType": "RESIDENTIAL", "fuelType": "ELECTRICITY", "geography": {"distributors": ["Ausgrid"]}}
    session = _Session([
        _Response(200, {"data": {"plans": [{**page, "planId": "A@EME", "displayName": "A"}]}, "meta": {"totalPages": 2}}),
        _Response(200, {"data": {"plans": [{**page, "planId": "B@EME", "displayName": "B"}]}, "meta": {"totalPages": 2}}),
    ])
    plans = asyncio.run(aer_plans.async_fetch_prd_plans(session, "red-energy"))
    assert [plan["planId"] for plan in plans] == ["A@EME", "B@EME"]
    assert session.calls[0][0] == (
        "https://cdr.energymadeeasy.gov.au/red-energy/cds-au/v1/energy/plans"
        "?fuelType=ELECTRICITY&page-size=1000&page=1"
    )
    assert session.calls[1][0].endswith("&page=2")
    assert session.calls[0][1]["headers"]["x-v"] == "1"
    assert session.calls[0][1]["allow_redirects"] is False

    detail = _Session([_Response(200, {"data": {"planId": "A@EME", "electricityContract": {}}})])
    assert asyncio.run(aer_plans.async_fetch_prd_plan(detail, "agl", "A@EME"))["planId"] == "A@EME"
    assert detail.calls[0][0] == "https://cdr.energymadeeasy.gov.au/agl/cds-au/v1/energy/plans/A%40EME"
    assert detail.calls[0][1]["headers"]["x-v"] == "3"

    with pytest.raises(aer_plans.PrdError, match="identity"):
        asyncio.run(aer_plans.async_fetch_prd_plan(_Session([_Response(200, {"data": {"planId": "B@EME"}})]), "agl", "A@EME"))
    with pytest.raises(aer_plans.PrdError, match="prd_http_302"):
        asyncio.run(aer_plans.async_fetch_prd_plan(_Session([_Response(302, {})]), "agl", "A@EME"))
    with pytest.raises(aer_plans.PrdError, match="prd_http_301"):
        asyncio.run(aer_plans.async_fetch_prd_plans(_Session([_Response(301, {})]), "agl"))
    with pytest.raises(aer_plans.PrdError, match="prd_http_404"):
        asyncio.run(aer_plans.async_fetch_prd_plans(_Session([_Response(404, {})]), "agl"))
    with pytest.raises(aer_plans.PrdError, match="invalid"):
        asyncio.run(aer_plans.async_fetch_prd_plans(_Session([_Response(200, {"data": {"plans": []}, "meta": {"totalPages": 99}})]), "agl"))
    with pytest.raises(aer_plans.PrdError, match="invalid_prd_plan"):
        asyncio.run(aer_plans.async_fetch_prd_plan(_Session([]), "agl", "bad id"))
