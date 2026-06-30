"""Regression tests for the tariff converter's leading-gap backfill.

Some providers (e.g. Flow Power's kwatch API) don't publish the very first
period of the horizon (00:00-00:30) until around midnight. The forward
carry-forward can't fill that first slot because no earlier price exists, so a
dedicated backfill pass fills leading gaps from the first published price.
These tests cover the nested ``_backfill_leading_gaps`` helper in isolation.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Callable


CONVERTER_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "power_sync"
    / "tariff_converter.py"
)


def _load_backfill() -> Callable[[dict, str], None]:
    """Extract the nested ``_backfill_leading_gaps`` helper without importing HA."""
    source = CONVERTER_PATH.read_text()
    tree = ast.parse(source)
    for outer in ast.walk(tree):
        if isinstance(outer, ast.FunctionDef) and outer.name == "_build_rolling_24h_tariff":
            for node in ast.walk(outer):
                if isinstance(node, ast.FunctionDef) and node.name == "_backfill_leading_gaps":
                    module = ast.Module(body=[node], type_ignores=[])
                    ast.fix_missing_locations(module)
                    # Provide a no-op logger the helper closes over.
                    class _Log:
                        def info(self, *a, **k):
                            pass

                    ns: dict[str, Any] = {"_LOGGER": _Log()}
                    exec(compile(module, str(CONVERTER_PATH), "exec"), ns)
                    return ns["_backfill_leading_gaps"]
    raise AssertionError("_backfill_leading_gaps not found in _build_rolling_24h_tariff")


_backfill = _load_backfill()


def test_single_leading_gap_filled_from_first_published():
    prices = {"PERIOD_00_00": None, "PERIOD_00_30": 0.30, "PERIOD_01_00": 0.31}
    _backfill(prices, "buy")
    assert prices["PERIOD_00_00"] == 0.30  # filled from first published slot
    assert prices["PERIOD_00_30"] == 0.30  # untouched
    assert prices["PERIOD_01_00"] == 0.31


def test_multiple_leading_gaps_filled():
    prices = {"PERIOD_00_00": None, "PERIOD_00_30": None, "PERIOD_01_00": 0.25}
    _backfill(prices, "buy")
    assert prices["PERIOD_00_00"] == 0.25
    assert prices["PERIOD_00_30"] == 0.25
    assert prices["PERIOD_01_00"] == 0.25


def test_interior_gaps_are_not_touched():
    # Only LEADING gaps are backfilled; interior gaps are the forward
    # carry-forward's responsibility and must remain untouched here.
    prices = {"PERIOD_00_00": 0.30, "PERIOD_00_30": None, "PERIOD_01_00": 0.31}
    _backfill(prices, "buy")
    assert prices["PERIOD_00_30"] is None


def test_no_leading_gap_is_unchanged():
    prices = {"PERIOD_00_00": 0.30, "PERIOD_00_30": 0.31}
    _backfill(prices, "buy")
    assert prices == {"PERIOD_00_00": 0.30, "PERIOD_00_30": 0.31}


def test_all_missing_left_alone():
    # Nothing to backfill from -> abort path elsewhere preserves last good tariff.
    prices = {"PERIOD_00_00": None, "PERIOD_00_30": None}
    _backfill(prices, "buy")
    assert prices == {"PERIOD_00_00": None, "PERIOD_00_30": None}


def test_zero_price_counts_as_published():
    # 0.0 is a valid published price, not "missing".
    prices = {"PERIOD_00_00": None, "PERIOD_00_30": 0.0, "PERIOD_01_00": 0.2}
    _backfill(prices, "sell")
    assert prices["PERIOD_00_00"] == 0.0
