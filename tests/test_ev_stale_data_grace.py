"""Phase 2: missing data may hold a start, but not end a running charge at once."""
from __future__ import annotations

from dataclasses import dataclass
import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]
automations = sys.modules.setdefault("power_sync.automations", types.ModuleType("power_sync.automations"))
automations.__path__ = [str(ROOT / "automations")]

stale = importlib.import_module("power_sync.automations.stale_data")
GRACE = stale.STALE_DATA_GRACE_SECONDS


def test_grace_runs_for_the_window_then_expires():
    holder = {}
    assert stale.missing_data_grace_expired(holder, "price", missing=True, now=1000) is False
    assert stale.missing_data_grace_remaining(holder, "price", now=1000 + 60) == GRACE - 60
    assert stale.missing_data_grace_expired(holder, "price", missing=True, now=1000 + GRACE - 1) is False
    assert stale.missing_data_grace_expired(holder, "price", missing=True, now=1000 + GRACE) is True


def test_returning_data_resets_the_clock():
    holder = {}
    stale.missing_data_grace_expired(holder, "price", missing=True, now=0)
    assert stale.missing_data_grace_expired(holder, "price", missing=False, now=GRACE - 1) is False
    assert stale.missing_data_grace_remaining(holder, "price") is None
    # A new gap gets a fresh window.
    assert stale.missing_data_grace_expired(holder, "price", missing=True, now=GRACE + 10) is False
    assert stale.missing_data_grace_expired(holder, "price", missing=True, now=2 * GRACE + 9) is False


def test_keys_are_independent_and_bad_clock_values_restart():
    holder = {"_missing_data_since": {"ev_soc": "garbage"}}
    assert stale.missing_data_grace_expired(holder, "ev_soc", missing=True, now=50) is False
    assert holder["_missing_data_since"]["ev_soc"] == 50
    stale.missing_data_grace_expired(holder, "price", missing=True, now=50)
    stale.missing_data_grace_expired(holder, "price", missing=False, now=60)
    assert "ev_soc" in holder["_missing_data_since"]
    # A future timestamp (clock jump) restarts instead of never expiring.
    holder["_missing_data_since"]["ev_soc"] = 10**12
    assert stale.missing_data_grace_expired(holder, "ev_soc", missing=True, now=100) is False
    assert holder["_missing_data_since"]["ev_soc"] == 100
