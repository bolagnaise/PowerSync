"""Regression coverage for OB-20: EPEX default export price must not be

the full retail import price.

``EPEXPriceCoordinator._async_update_data`` (in coordinator.py) converts
EPEX Predictor API entries into Amber-compatible price dicts. The API only
returns a "total" field per entry — the final consumer price with
surcharge/tax already applied server-side (confirmed live: total = (raw +
surcharge) * (1 + tax_percent/100), no separate wholesale/spot field is
present in the payload). When no Fixed Export Rate is configured (the
default on a fresh install, see CONF_EPEX_EXPORT_RATE default of 0.0 in
__init__.py), the coordinator must NOT value exports at the retail total —
that made the optimizer export midday energy it should hold for the
evening peak.

Uses the AST source-extraction pattern (see test_sungrow_curtailment_runtime.py)
to exercise the real _async_update_data method in isolation, since importing
coordinator.py directly requires a full Home Assistant environment.
"""

from __future__ import annotations

import ast
import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"
STRINGS_PATHS = (
    ROOT / "custom_components" / "power_sync" / "strings.json",
    ROOT / "custom_components" / "power_sync" / "translations" / "en.json",
)


def _method_source(class_name: str, method_name: str) -> str:
    source = COORDINATOR_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == method_name:
                    segment = ast.get_source_segment(source, item)
                    assert segment is not None
                    finite = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "_finite_float")
                    return ast.unparse(finite) + "\n" + segment
    raise AssertionError(f"{class_name}.{method_name} not found")


def _values_for_key(value: Any, key: str) -> list[str]:
    matches: list[str] = []
    if isinstance(value, dict):
        for item_key, item_value in value.items():
            if item_key == key and isinstance(item_value, str):
                matches.append(item_value)
            matches.extend(_values_for_key(item_value, key))
    elif isinstance(value, list):
        for item in value:
            matches.extend(_values_for_key(item, key))
    return matches


class UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class _FakeEPEXClient:
    def __init__(self, prices: list[dict], raw_prices: list[dict] | None = None) -> None:
        self._prices = prices
        self._raw_prices = raw_prices if raw_prices is not None else prices
        self.calls: list[tuple] = []

    async def get_prices(self, region: str, surcharge: float, tax_percent: float) -> list[dict]:
        self.calls.append((region, surcharge, tax_percent))
        return self._raw_prices if surcharge == 0 and tax_percent == 0 else self._prices


FIXED_NOW = datetime(2026, 7, 8, 10, 30, tzinfo=timezone.utc)


def _make_self(
    export_rate: float,
    prices: list[dict],
    warnings: list,
    export_source: str | None = None,
    raw_prices: list[dict] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        region="DE",
        _surcharge=8.0,
        _tax_percent=19.0,
        _export_rate=export_rate,
        _client=_FakeEPEXClient(prices, raw_prices),
        _export_source=export_source,
        _warned_export_rate_unset=False,
    )


def _run_update_data(self_obj: SimpleNamespace, warnings: list) -> dict:
    namespace: dict[str, Any] = {
        "Any": Any,
        "datetime": datetime,
        "timedelta": timedelta,
        "math": math,
        "UpdateFailed": UpdateFailed,
        "dt_util": SimpleNamespace(utcnow=lambda: FIXED_NOW, UTC=timezone.utc),
        "_LOGGER": SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: warnings.append((a, k)),
            error=lambda *a, **k: None,
        ),
    }
    exec(_method_source("EPEXPriceCoordinator", "_async_update_data"), namespace)
    method = namespace["_async_update_data"]
    return asyncio.run(method(self_obj))


# A single current-interval entry: retail "total" (surcharge + tax already
# applied server-side per the EPEX Predictor API) is 27.0 ct/kWh, well above
# a plausible wholesale/spot value (~8 ct/kWh). This is the only field the
# live API actually returns per entry (confirmed via direct API probe):
# {"startsAt": ..., "total": ...} — no separate wholesale/spot component.
CURRENT_INTERVAL_ENTRY = {
    "startsAt": "2026-07-08T10:00:00+00:00",
    "total": 27.0,
}


def test_epex_default_export_price_is_not_retail_import_price():
    """No configured export rate: export must not equal -retail_total."""
    warnings: list = []
    self_obj = _make_self(export_rate=0.0, prices=[CURRENT_INTERVAL_ENTRY], warnings=warnings)

    data = _run_update_data(self_obj, warnings)

    export_entries = [e for e in data["current"] if e["channelType"] == "feedIn"]
    import_entries = [e for e in data["current"] if e["channelType"] == "general"]

    assert import_entries[0]["perKwh"] == 27.0
    export_ct = export_entries[0]["perKwh"]

    # The bug: export_ct == -27.0 (the full retail/import price).
    assert export_ct != -27.0, "export priced at full retail import rate (money-losing default)"

    # No wholesale/spot component is separable from the EPEX payload (only
    # "total" is returned), so the safe default is 0 — never assume an
    # export value that isn't actually known.
    assert export_ct == 0.0

    # A one-time warning must be logged so the user knows to configure a
    # Fixed Export Rate / export price entity.
    assert len(warnings) == 1


def test_epex_default_export_warning_is_logged_only_once_per_coordinator():
    warnings: list = []
    self_obj = _make_self(export_rate=0.0, prices=[CURRENT_INTERVAL_ENTRY], warnings=warnings)

    asyncio.run(_run_update_data_async(self_obj, warnings))
    asyncio.run(_run_update_data_async(self_obj, warnings))

    assert len(warnings) == 1


async def _run_update_data_async(self_obj: SimpleNamespace, warnings: list) -> dict:
    # Reuse the sync helper's exec plumbing without re-running asyncio.run
    # inside an already-running loop.
    namespace: dict[str, Any] = {
        "Any": Any,
        "datetime": datetime,
        "timedelta": timedelta,
        "math": math,
        "UpdateFailed": UpdateFailed,
        "dt_util": SimpleNamespace(utcnow=lambda: FIXED_NOW, UTC=timezone.utc),
        "_LOGGER": SimpleNamespace(
            info=lambda *a, **k: None,
            debug=lambda *a, **k: None,
            warning=lambda *a, **k: warnings.append((a, k)),
            error=lambda *a, **k: None,
        ),
    }
    exec(_method_source("EPEXPriceCoordinator", "_async_update_data"), namespace)
    method = namespace["_async_update_data"]
    return await method(self_obj)


def test_epex_configured_export_rate_branch_is_unchanged():
    """A configured Fixed Export Rate must still be used verbatim (unchanged branch)."""
    warnings: list = []
    self_obj = _make_self(export_rate=8.5, prices=[CURRENT_INTERVAL_ENTRY], warnings=warnings)

    data = _run_update_data(self_obj, warnings)

    export_entries = [e for e in data["current"] if e["channelType"] == "feedIn"]
    assert export_entries[0]["perKwh"] == -8.5
    # Configured-rate branch never needs the "not configured" warning.
    assert len(warnings) == 0


def test_epex_raw_export_uses_zero_adjustment_request_and_matching_intervals():
    warnings: list = []
    raw_prices = [
        {"startsAt": "2026-07-08T10:00:00+00:00", "total": 7.5},
        {"startsAt": "2026-07-08T11:00:00+00:00", "total": -2.0},
    ]
    self_obj = _make_self(
        export_rate=8.5,
        prices=[
            CURRENT_INTERVAL_ENTRY,
            {"startsAt": "2026-07-08T11:00:00+00:00", "total": 31.0},
        ],
        warnings=warnings,
        export_source="raw_wholesale",
        raw_prices=raw_prices,
    )

    data = _run_update_data(self_obj, warnings)

    assert self_obj._client.calls == [("DE", 8.0, 19.0), ("DE", 0.0, 0.0)]
    exports = [
        entry for entry in data["current"] + data["forecast"]
        if entry["channelType"] == "feedIn"
    ]
    assert [entry["perKwh"] for entry in exports] == [-7.5, 2.0]


def test_epex_raw_export_missing_or_invalid_interval_fails_closed_to_zero():
    warnings: list = []
    self_obj = _make_self(
        export_rate=8.5,
        prices=[CURRENT_INTERVAL_ENTRY],
        warnings=warnings,
        export_source="raw_wholesale",
        raw_prices=[{"startsAt": "2026-07-08T10:00:00+00:00", "total": "nan"}],
    )

    data = _run_update_data(self_obj, warnings)

    export = next(entry for entry in data["current"] if entry["channelType"] == "feedIn")
    assert export["perKwh"] == 0.0


def test_epex_export_source_copy_explains_raw_wholesale_semantics():
    for path in STRINGS_PATHS:
        descriptions = [
            value
            for value in _values_for_key(
                json.loads(path.read_text()),
                "epex_export_source",
            )
            if "Raw EPEX wholesale" in value
        ]
        assert descriptions
        for description in descriptions:
            assert "EUR ct/kWh" in description
            assert "tax are excluded" in description
            assert "modelled" in description


def test_epex_region_copy_lists_only_supported_zone_examples():
    for path in STRINGS_PATHS:
        descriptions = [
            value
            for value in _values_for_key(
                json.loads(path.read_text()),
                "epex_region",
            )
            if "supported EPEX bidding zone" in value
        ]
        assert descriptions
        for description in descriptions:
            assert "FR" not in description
            assert "DK1" in description
            assert "SE1-SE4" in description


def test_epex_belgium_preserves_native_quarter_hour_boundaries():
    warnings: list = []
    self_obj = _make_self(
        export_rate=8.5,
        prices=[
            {
                "startsAt": "2026-07-08T10:30:00+00:00",
                "total": 21.0,
            },
            {
                "startsAt": "2026-07-08T10:45:00+00:00",
                "total": 18.0,
            },
            {
                "startsAt": "2026-07-08T11:00:00+00:00",
                "total": 15.0,
            },
        ],
        warnings=warnings,
    )
    self_obj.region = "BE"

    data = _run_update_data(self_obj, warnings)
    imports = [
        entry
        for entry in data["current"] + data["forecast"]
        if entry["channelType"] == "general"
    ]

    assert [entry["duration"] for entry in imports] == [15, 15, 15]
    assert imports[0]["startTime"] == "2026-07-08T10:30:00+00:00"
    assert imports[0]["endTime"] == "2026-07-08T10:45:00+00:00"
    assert imports[0]["nemTime"] == imports[0]["endTime"]
    assert imports[1]["startTime"] == imports[0]["endTime"]


def test_epex_explicit_interval_end_takes_precedence_over_inference():
    warnings: list = []
    self_obj = _make_self(
        export_rate=8.5,
        prices=[
            {
                "startsAt": "2026-07-08T10:30:00+00:00",
                "endsAt": "2026-07-08T10:50:00+00:00",
                "total": 21.0,
            },
            {
                "startsAt": "2026-07-08T11:00:00+00:00",
                "total": 15.0,
            },
        ],
        warnings=warnings,
    )
    self_obj.region = "BE"

    data = _run_update_data(self_obj, warnings)
    current_import = next(
        entry
        for entry in data["current"]
        if entry["channelType"] == "general"
    )

    assert current_import["duration"] == 20
    assert current_import["endTime"] == "2026-07-08T10:50:00+00:00"


def test_native_retail_coverage_excludes_missing_total_and_missing_quarter_hour():
    warnings = []
    coordinator = _make_self(export_rate=0, prices=[
        {"startsAt": "2026-07-08T10:30:00+00:00"},
        {"startsAt": "2026-07-08T11:00:00+00:00", "total": 0},
    ], warnings=warnings)
    coordinator.region = "BE"
    result = _run_update_data(coordinator, warnings)
    assert all(row["channelType"] != "general" for row in result["current"])
    current_export = next(row for row in result["current"] if row["channelType"] == "feedIn")
    future = next(row for row in result["forecast"] if row["channelType"] == "general")
    assert current_export["endTime"] == "2026-07-08T10:45:00+00:00"
    assert future["perKwh"] == 0
    assert all(row["price_available"] is False for row in result["current"] if row["channelType"] == "feedIn")


def test_epex_first_future_rate_is_never_published_as_current():
    warnings = []
    coordinator = _make_self(export_rate=8, prices=[
        {"startsAt": "2026-07-08T11:00:00+00:00", "total": 0},
    ], warnings=warnings)
    result = _run_update_data(coordinator, warnings)
    assert result["current"] == []
    assert any(row["perKwh"] == 0 for row in result["forecast"] if row["channelType"] == "general")


def test_epex_raw_wholesale_requires_one_unambiguous_finite_value_per_start():
    for values, expected in [
        ([True], None), ([float("nan")], None), ([3.5, -4, 3.5], None),
        ([-4, 3.5], None), ([False, 0], None), ([0, 0], 0), ([-4, -4.0], 4),
    ]:
        raw = [{"startsAt": CURRENT_INTERVAL_ENTRY["startsAt"], "total": value} for value in values]
        warnings = []
        coordinator = _make_self(export_rate=0, prices=[CURRENT_INTERVAL_ENTRY], warnings=warnings,
                                 export_source="raw_wholesale", raw_prices=raw)
        result = _run_update_data(coordinator, warnings)
        export = next(row for row in result["current"] if row["channelType"] == "feedIn")
        assert export["price_available"] is (expected is not None)
        assert export["perKwh"] == (expected if expected is not None else 0)
