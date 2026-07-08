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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PATH = ROOT / "custom_components" / "power_sync" / "coordinator.py"


def _method_source(class_name: str, method_name: str) -> str:
    source = COORDINATOR_PATH.read_text()
    module = ast.parse(source)
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.AsyncFunctionDef) and item.name == method_name:
                    segment = ast.get_source_segment(source, item)
                    assert segment is not None
                    return segment
    raise AssertionError(f"{class_name}.{method_name} not found")


class UpdateFailed(Exception):
    """Stand-in for homeassistant.helpers.update_coordinator.UpdateFailed."""


class _FakeEPEXClient:
    def __init__(self, prices: list[dict]) -> None:
        self._prices = prices
        self.calls: list[tuple] = []

    async def get_prices(self, region: str, surcharge: float, tax_percent: float) -> list[dict]:
        self.calls.append((region, surcharge, tax_percent))
        return self._prices


FIXED_NOW = datetime(2026, 7, 8, 10, 30, tzinfo=timezone.utc)


def _make_self(export_rate: float, prices: list[dict], warnings: list) -> SimpleNamespace:
    return SimpleNamespace(
        region="DE",
        _surcharge=8.0,
        _tax_percent=19.0,
        _export_rate=export_rate,
        _client=_FakeEPEXClient(prices),
        _warned_export_rate_unset=False,
    )


def _run_update_data(self_obj: SimpleNamespace, warnings: list) -> dict:
    namespace: dict[str, Any] = {
        "Any": Any,
        "datetime": datetime,
        "timedelta": timedelta,
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
