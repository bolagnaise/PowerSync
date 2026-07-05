"""Unit tests for FlowPowerTWAPTracker billing-period-anchored TWAP (B2).

Flow Power settles PEA against the time-weighted average price over the *billing
period*, not a flat trailing window. The tracker blends billing-period-to-date
actuals with the trailing mean (a forward proxy for the rest of the period),
weighted by how far through the period we are. These tests pin that behaviour:
early-period stability (no regression vs the old 30-day mean), monotonic
convergence to the billing TWAP, the insufficient-data fallbacks, the billing
day-of-month anchor math, and pruning retention.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules.setdefault("power_sync", _ps)

# A single mutable clock drives both `time.time()` (patched per test) and the
# homeassistant.util.dt stubs, so billing-period math is deterministic.
AEST = timezone(timedelta(hours=10))
_CLOCK = {"ts": datetime(2026, 7, 21, 12, 0, tzinfo=AEST).timestamp()}


def _install_homeassistant_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")
    ha_components = types.ModuleType("homeassistant.components")
    ha_recorder = types.ModuleType("homeassistant.components.recorder")
    ha_recorder_history = types.ModuleType("homeassistant.components.recorder.history")

    class DataUpdateCoordinator:
        def __init__(self, hass, *args, **kwargs) -> None:
            self.hass = hass
            self.data = None

    class Store:
        def __init__(self, *args, **kwargs) -> None:
            self.data = None

        async def async_load(self):
            return self.data

        async def async_save(self, data):
            self.data = data

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
    ha_update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = Store
    ha_dt.now = lambda: datetime.fromtimestamp(_CLOCK["ts"], AEST)
    ha_dt.utcnow = lambda: datetime.fromtimestamp(_CLOCK["ts"], timezone.utc)
    ha_dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts, timezone.utc)
    ha_dt.as_local = lambda dt: dt.astimezone(AEST)
    ha_recorder.get_instance = lambda hass: None
    ha_recorder_history.get_significant_states = lambda *a, **k: []

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.components"] = ha_components
    sys.modules["homeassistant.components.recorder"] = ha_recorder
    sys.modules["homeassistant.components.recorder.history"] = ha_recorder_history
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_aiohttp_client
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt


_install_homeassistant_stubs()
sys.modules.pop("power_sync.coordinator", None)

from power_sync import coordinator as coord_mod  # noqa: E402
from power_sync.coordinator import FlowPowerTWAPTracker  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_clock(monkeypatch):
    """Make the tracker's `time.time()` read the shared deterministic clock."""
    monkeypatch.setattr(
        coord_mod, "time", SimpleNamespace(time=lambda: _CLOCK["ts"])
    )
    yield


def _set_now(dt: datetime) -> None:
    _CLOCK["ts"] = dt.timestamp()


def _make_tracker(billing_day: int = 1) -> FlowPowerTWAPTracker:
    return FlowPowerTWAPTracker(
        SimpleNamespace(), "NSW1", "test_entry", billing_day=billing_day
    )


def _seed(tracker, now, prior_price, period_price, prior_days=35, per_day=48):
    """Fill history with `prior_price` before the billing start, `period_price` after."""
    period_start = datetime.fromtimestamp(
        tracker._billing_period_start_ts(now.timestamp()), AEST
    )
    step = 86400 / per_day
    t = now - timedelta(days=prior_days)
    while t <= now:
        price = period_price if t >= period_start else prior_price
        tracker._price_history.append({"ts": t.timestamp(), "price": price})
        t += timedelta(seconds=step)


# --------------------------------------------------------------------------- #
# Billing-day anchor math
# --------------------------------------------------------------------------- #

def test_billing_period_start_uses_this_month_when_past_billing_day():
    tracker = _make_tracker(billing_day=1)
    now = datetime(2026, 7, 21, 12, 0, tzinfo=AEST)
    start = datetime.fromtimestamp(
        tracker._billing_period_start_ts(now.timestamp()), AEST
    )
    assert (start.year, start.month, start.day) == (2026, 7, 1)
    assert (start.hour, start.minute) == (0, 0)


def test_billing_period_start_rolls_back_when_before_billing_day():
    tracker = _make_tracker(billing_day=15)
    now = datetime(2026, 7, 3, 9, 0, tzinfo=AEST)  # before the 15th
    start = datetime.fromtimestamp(
        tracker._billing_period_start_ts(now.timestamp()), AEST
    )
    assert (start.year, start.month, start.day) == (2026, 6, 15)


def test_billing_day_is_clamped_to_valid_range():
    assert _make_tracker(billing_day=31).billing_day == 28
    assert _make_tracker(billing_day=0).billing_day == 1
    assert _make_tracker(billing_day=15).billing_day == 15


# --------------------------------------------------------------------------- #
# The blend: early stability -> convergence to billing TWAP
# --------------------------------------------------------------------------- #

def test_twap_starts_near_trailing_and_converges_to_billing_period():
    """Seeded from GreatEagle's log: trailing 8.66c, billing period 18.43c."""
    results = {}
    for day in (2, 10, 21, 28):
        now = datetime(2026, 7, day, 12, 0, tzinfo=AEST)
        _set_now(now)
        tracker = _make_tracker(billing_day=1)
        _seed(tracker, now, prior_price=8.66, period_price=18.43)
        results[day] = tracker._calculate_twap()

    # Early in the period we stay close to the old trailing behaviour...
    assert results[2] < 11.0
    # ...and climb monotonically toward the true billing-period TWAP.
    assert results[2] < results[10] < results[21] < results[28]
    # By period end it is materially closer to 18.43 than the old 8.66c.
    assert results[28] > 14.0


def test_twap_never_regresses_below_old_flat_mean_early():
    """Old code returned the flat mean of all history; new code must not do worse."""
    now = datetime(2026, 7, 2, 12, 0, tzinfo=AEST)
    _set_now(now)
    tracker = _make_tracker(billing_day=1)
    _seed(tracker, now, prior_price=8.66, period_price=18.43)
    flat_mean = round(
        sum(e["price"] for e in tracker._price_history)
        / len(tracker._price_history),
        2,
    )
    # Effective TWAP is a convex blend of mtd (higher) and trailing, so it is
    # >= the flat all-history mean here.
    assert tracker._calculate_twap() >= flat_mean - 0.01


# --------------------------------------------------------------------------- #
# Fallbacks
# --------------------------------------------------------------------------- #

def test_returns_none_below_min_samples():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=AEST)
    _set_now(now)
    tracker = _make_tracker()
    tracker._price_history = [
        {"ts": now.timestamp(), "price": 10.0} for _ in range(5)
    ]
    assert tracker._calculate_twap() is None
    assert tracker.using_fallback is True


def test_falls_back_to_trailing_when_billing_period_too_young():
    """Enough total samples, but < MIN_TWAP_SAMPLES since the billing start."""
    now = datetime(2026, 7, 1, 0, 20, tzinfo=AEST)  # 20 min into the period
    _set_now(now)
    tracker = _make_tracker(billing_day=1)
    # 200 prior-month samples, only a couple inside the young period.
    base = now - timedelta(days=20)
    for i in range(200):
        tracker._price_history.append(
            {"ts": (base + timedelta(hours=i)).timestamp(), "price": 9.0}
        )
    tracker._price_history.append({"ts": now.timestamp(), "price": 40.0})
    trailing, _ = tracker._mean_since(0.0)
    assert tracker._calculate_twap() == trailing  # not skewed by the 40c outlier


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #

def test_prune_retains_trailing_window_and_drops_older():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=AEST)
    _set_now(now)
    tracker = _make_tracker(billing_day=1)
    old = now - timedelta(days=60)   # well beyond retention
    recent = now - timedelta(days=10)
    tracker._price_history = [
        {"ts": old.timestamp(), "price": 5.0},
        {"ts": recent.timestamp(), "price": 12.0},
        {"ts": now.timestamp(), "price": 20.0},
    ]
    tracker._prune_history()
    kept = [e["ts"] for e in tracker._price_history]
    assert old.timestamp() not in kept
    assert recent.timestamp() in kept
    assert now.timestamp() in kept


def test_observability_properties_report_components():
    now = datetime(2026, 7, 21, 12, 0, tzinfo=AEST)
    _set_now(now)
    tracker = _make_tracker(billing_day=1)
    _seed(tracker, now, prior_price=8.66, period_price=18.43)
    assert tracker.mtd_twap == pytest.approx(18.43, abs=0.2)
    assert tracker.trailing_twap is not None
    assert 0.0 <= tracker.period_progress <= 1.0
    # 20 days into a 30-day nominal period -> ~0.67
    assert tracker.period_progress == pytest.approx(0.667, abs=0.02)
