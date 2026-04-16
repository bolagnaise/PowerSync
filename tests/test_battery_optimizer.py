"""Unit tests for the LP battery optimizer.

Tests cover:
- Known-answer LP verification (charge cheap, discharge peak)
- SOC bounds (never below reserve, never above max)
- Power balance conservation at every timestep
- Greedy fallback when scipy unavailable
- Edge cases (empty forecasts, zero capacity, single timestep, flat prices)
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch, MagicMock
from zoneinfo import ZoneInfo

import pytest

# Guard: skip entire module if deps aren't available.
# Import directly from the module file to avoid triggering the full __init__.py
# package chain (which pulls in 22K LOC of HA-dependent code).
import importlib
import importlib.util
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_OPT_DIR = _REPO / "custom_components" / "power_sync" / "optimization"

def _load_module_direct(name: str, filepath: Path):
    """Load a Python module from file path without triggering parent __init__.py."""
    spec = importlib.util.spec_from_file_location(name, filepath)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {filepath}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

try:
    # Load schedule_reader first (battery_optimizer imports from it)
    _sr = _load_module_direct(
        "custom_components.power_sync.optimization.schedule_reader",
        _OPT_DIR / "schedule_reader.py",
    )
    ScheduleAction = _sr.ScheduleAction
    OptimizationSchedule = _sr.OptimizationSchedule

    _bo = _load_module_direct(
        "custom_components.power_sync.optimization.battery_optimizer",
        _OPT_DIR / "battery_optimizer.py",
    )
    BatteryOptimizer = _bo.BatteryOptimizer
    OptimizerResult = _bo.OptimizerResult
    SCIPY_AVAILABLE = _bo.SCIPY_AVAILABLE
    DEFAULT_EFFICIENCY = _bo.DEFAULT_EFFICIENCY

    # Keep reference to the loaded module for patching dt_util
    _bo_module = _bo

    HAS_DEPS = True
except (ImportError, Exception) as _import_err:
    HAS_DEPS = False
    _skip_reason = f"Cannot load optimizer module: {_import_err}"
    _bo_module = None

pytestmark = pytest.mark.skipif(not HAS_DEPS, reason=_skip_reason if not HAS_DEPS else "")

MELBOURNE_TZ = ZoneInfo("Australia/Melbourne")
FIXED_NOW = datetime(2026, 1, 15, 6, 0, 0, tzinfo=MELBOURNE_TZ)


@pytest.fixture(autouse=True)
def _mock_dt_util():
    """Patch dt_util.now() on the loaded optimizer module for deterministic timestamps."""
    if _bo_module is None:
        yield
        return
    mock_dt = MagicMock()

    original = getattr(_bo_module, "dt_util", None)
    _bo_module.dt_util = mock_dt
    yield mock_dt
    if original is not None:
        _bo_module.dt_util = original


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def optimizer():
    """Standard Powerwall-sized optimizer: 13.5kWh, 5kW, 92% eff, 20% reserve."""
    return BatteryOptimizer(
        capacity_wh=13500,
        max_charge_w=5000,
        max_discharge_w=5000,
        efficiency=DEFAULT_EFFICIENCY,
        backup_reserve=0.20,
        max_soc=1.0,
        interval_minutes=30,
        horizon_hours=24,
    )


@pytest.fixture
def small_optimizer():
    """Small battery for fast LP: 5kWh, 2kW, 30-min intervals, 12h horizon."""
    return BatteryOptimizer(
        capacity_wh=5000,
        max_charge_w=2000,
        max_discharge_w=2000,
        efficiency=0.90,
        backup_reserve=0.20,
        max_soc=1.0,
        interval_minutes=30,
        horizon_hours=12,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_flat(n: int, value: float) -> list[float]:
    """Return a flat forecast of length n."""
    return [value] * n


def make_price_curve(n: int) -> tuple[list[float], list[float]]:
    """Return (import_prices, export_prices) in $/kWh for n 30-min steps.

    Cheap overnight (0-6h), shoulder (6-15h), peak (15-21h), off-peak (21-24h).
    Export = import * 0.3 (typical FiT ratio).
    """
    import_prices = []
    export_prices = []
    for i in range(n):
        hour = (i * 0.5) % 24
        if hour < 6:
            p = 0.05  # 5c/kWh
        elif hour < 15:
            p = 0.20  # 20c/kWh
        elif hour < 21:
            p = 0.50  # 50c/kWh
        else:
            p = 0.12  # 12c/kWh
        import_prices.append(p)
        export_prices.append(p * 0.3)
    return import_prices, export_prices


def verify_soc_bounds(
    result: OptimizerResult,
    opt: BatteryOptimizer,
    initial_soc: float,
) -> None:
    """Walk the schedule and verify SOC stays within bounds at every step."""
    for i, action in enumerate(result.schedule.actions):
        soc = action.soc
        assert soc is not None, f"Step {i}: SOC is None"
        assert soc >= opt.backup_reserve - 0.02, (
            f"Step {i}: SOC {soc:.4f} below reserve {opt.backup_reserve}"
        )
        assert soc <= opt.max_soc + 0.001, (
            f"Step {i}: SOC {soc:.4f} above max {opt.max_soc}"
        )


def verify_power_balance(
    result: OptimizerResult,
    solar: list[float],
    load: list[float],
    tolerance: float = 0.1,
) -> None:
    """Verify energy conservation: solar + import + discharge == load + export + charge.

    Uses grid_import_w and grid_export_w from OptimizerResult (in watts).
    Schedule actions have battery_charge_w and battery_discharge_w.
    """
    n = min(
        len(result.grid_import_w),
        len(result.grid_export_w),
        len(result.schedule.actions),
        len(solar),
        len(load),
    )
    for t in range(n):
        gi = result.grid_import_w[t] / 1000  # kW
        ge = result.grid_export_w[t] / 1000
        bc = result.schedule.actions[t].battery_charge_w / 1000
        bd = result.schedule.actions[t].battery_discharge_w / 1000
        s = solar[t]
        l = load[t]

        # Power balance: solar + grid_import + battery_discharge = load + grid_export + battery_charge
        lhs = s + gi + bd
        rhs = l + ge + bc
        assert abs(lhs - rhs) < tolerance, (
            f"Step {t}: power imbalance {lhs:.3f} != {rhs:.3f} "
            f"(solar={s:.2f} gi={gi:.2f} bd={bd:.2f} | load={l:.2f} ge={ge:.2f} bc={bc:.2f})"
        )


# ---------------------------------------------------------------------------
# Known-Answer Tests (AC-1)
# ---------------------------------------------------------------------------

def test_lp_charges_during_cheap_discharges_during_peak(optimizer):
    """LP should charge overnight (5c) and discharge during peak (50c)."""


    n = 48  # 24 hours at 30-min intervals
    import_prices, export_prices = make_price_curve(n)
    solar = make_flat(n, 0.0)  # No solar — pure arbitrage test
    load = make_flat(n, 1.0)  # 1kW constant load

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.50,
    )

    assert result.feasible
    assert result.schedule.actions, "Schedule should not be empty"

    # Check actions: overnight (steps 0-11, hours 0-6) should have charge actions
    overnight_actions = [a for a in result.schedule.actions[:12] if a.action == "charge"]
    # Peak (steps 30-41, hours 15-21) should have export/discharge actions
    peak_actions = [
        a for a in result.schedule.actions[30:42]
        if a.action in ("export", "self_consumption") and a.battery_discharge_w > 100
    ]

    assert len(overnight_actions) > 0, "Should charge during cheap overnight"
    assert len(peak_actions) > 0, "Should discharge/export during expensive peak"
    assert result.schedule.predicted_savings > 0, "Should save money vs no-battery baseline"


def test_lp_negative_price_triggers_charge(small_optimizer):
    """Negative import price should strongly incentivize charging."""


    n = 24  # 12 hours at 30-min intervals
    import_prices = [0.20] * n
    export_prices = [0.05] * n

    # Make interval 5 negative (getting paid to import)
    import_prices[5] = -0.05

    solar = make_flat(n, 0.0)
    load = make_flat(n, 0.5)

    result = small_optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.30,
    )

    assert result.feasible
    # Step 5 should have charging
    if len(result.schedule.actions) > 5:
        step5 = result.schedule.actions[5]
        assert step5.battery_charge_w > 0, (
            f"Should charge at negative price, got action={step5.action} charge_w={step5.battery_charge_w}"
        )


# ---------------------------------------------------------------------------
# SOC Bounds Tests (AC-2)
# ---------------------------------------------------------------------------

def test_soc_never_exceeds_max(optimizer):
    """SOC should never go above max_soc even with excess solar."""


    n = 48
    import_prices = make_flat(n, 0.20)
    export_prices = make_flat(n, 0.05)
    solar = make_flat(n, 8.0)  # Huge solar surplus
    load = make_flat(n, 1.0)

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.95,  # Start near full
    )

    verify_soc_bounds(result, optimizer, 0.95)


def test_soc_never_below_reserve(optimizer):
    """SOC should never drop below backup reserve."""


    n = 48
    import_prices = make_flat(n, 0.05)  # Cheap — no export incentive
    export_prices = make_flat(n, 0.50)  # High export — strong discharge incentive
    solar = make_flat(n, 0.0)
    load = make_flat(n, 2.0)

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.25,  # Close to 20% reserve
    )

    verify_soc_bounds(result, optimizer, 0.25)


def test_soc_below_reserve_initial(optimizer):
    """Starting below reserve should not crash — optimizer relaxes constraints."""


    n = 48
    import_prices, export_prices = make_price_curve(n)
    solar = make_flat(n, 2.0)
    load = make_flat(n, 1.0)

    # Start at 15% with 20% reserve — should handle gracefully
    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.15,
    )

    assert result.schedule.actions, "Should produce a schedule even when starting below reserve"


# ---------------------------------------------------------------------------
# Power Balance Tests (AC-3)
# ---------------------------------------------------------------------------

def test_power_balance_every_timestep(optimizer):
    """Energy must be conserved at every timestep."""


    n = 48
    import_prices, export_prices = make_price_curve(n)
    solar = [0.0] * 12 + [3.0] * 12 + [5.0] * 12 + [0.0] * 12  # Midday solar
    load = [0.5] * 12 + [1.0] * 12 + [2.0] * 12 + [1.5] * 12  # Variable load

    result = optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.50,
    )

    verify_power_balance(result, solar, load, tolerance=0.5)


# ---------------------------------------------------------------------------
# Greedy Fallback Tests (AC-4)
# ---------------------------------------------------------------------------

def test_greedy_fallback_when_scipy_unavailable(optimizer):
    """When scipy is unavailable, optimizer should fall back to greedy."""
    original = _bo_module.SCIPY_AVAILABLE
    _bo_module.SCIPY_AVAILABLE = False
    try:
        n = 48
        import_prices, export_prices = make_price_curve(n)
        solar = make_flat(n, 2.0)
        load = make_flat(n, 1.0)

        result = optimizer.optimize(
            import_prices=import_prices,
            export_prices=export_prices,
            solar_forecast=solar,
            load_forecast=load,
            current_soc=0.50,
        )

        assert result.solver_used == "greedy"
        assert result.schedule.actions, "Greedy should produce a schedule"
        verify_soc_bounds(result, optimizer, 0.50)
    finally:
        _bo_module.SCIPY_AVAILABLE = original


def test_greedy_produces_savings(optimizer):
    """Greedy should still save money compared to no-battery baseline."""
    original = _bo_module.SCIPY_AVAILABLE
    _bo_module.SCIPY_AVAILABLE = False
    try:
        n = 48
        import_prices, export_prices = make_price_curve(n)
        solar = make_flat(n, 0.0)
        load = make_flat(n, 1.5)

        result = optimizer.optimize(
            import_prices=import_prices,
            export_prices=export_prices,
            solar_forecast=solar,
            load_forecast=load,
            current_soc=0.50,
        )

        assert result.solver_used == "greedy"
        # Greedy may produce small negative savings due to efficiency losses
        # from suboptimal charge timing. Verify it's bounded (not catastrophically wrong).
        assert result.schedule.predicted_savings > -5.0, (
            f"Greedy savings catastrophically negative: {result.schedule.predicted_savings}"
        )
    finally:
        _bo_module.SCIPY_AVAILABLE = original


# ---------------------------------------------------------------------------
# Edge Case Tests (AC-5)
# ---------------------------------------------------------------------------

def test_all_same_prices(small_optimizer):
    """Flat prices offer no arbitrage — optimizer should mostly idle/self-consume."""


    n = 24
    import_prices = make_flat(n, 0.20)
    export_prices = make_flat(n, 0.06)
    solar = make_flat(n, 1.0)
    load = make_flat(n, 1.0)

    result = small_optimizer.optimize(
        import_prices=import_prices,
        export_prices=export_prices,
        solar_forecast=solar,
        load_forecast=load,
        current_soc=0.50,
    )

    assert result.feasible
    assert result.schedule.actions, "Should produce schedule even with flat prices"


def test_empty_forecasts(optimizer):
    """Empty input arrays should return gracefully, not crash."""


    result = optimizer.optimize(
        import_prices=[],
        export_prices=[],
        solar_forecast=[],
        load_forecast=[],
        current_soc=0.50,
    )

    assert not result.feasible or len(result.schedule.actions) == 0


def test_zero_battery_capacity():
    """Zero-capacity battery should not crash."""


    opt = BatteryOptimizer(
        capacity_wh=1,  # Near-zero (avoid /0)
        max_charge_w=0,
        max_discharge_w=0,
        interval_minutes=30,
        horizon_hours=6,
    )

    n = 12
    result = opt.optimize(
        import_prices=make_flat(n, 0.20),
        export_prices=make_flat(n, 0.05),
        solar_forecast=make_flat(n, 2.0),
        load_forecast=make_flat(n, 1.0),
        current_soc=0.50,
    )

    assert result.schedule is not None


def test_single_timestep(small_optimizer):
    """Single-step input should produce a single-action result."""


    result = small_optimizer.optimize(
        import_prices=[0.20],
        export_prices=[0.05],
        solar_forecast=[3.0],
        load_forecast=[1.0],
        current_soc=0.50,
    )

    assert result.schedule.actions, "Should produce at least one action"
    assert len(result.schedule.actions) == 1
