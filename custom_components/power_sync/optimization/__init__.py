"""
Optimization package for PowerSync battery scheduling.

This module provides:
- BatteryOptimizer: Built-in LP optimizer (HiGHS/highspy) with greedy fallback
- OptimizationCoordinator: Main coordinator for optimization
- ScheduleExecutor: Executes battery commands
- LoadEstimator, SolcastForecaster: Forecast data sources for optimizer
- EVCoordinator: Smart EV charging coordination
"""
from .load_estimator import LoadEstimator, SolcastForecaster
from .executor import ScheduleExecutor, BatteryAction, ExecutionStatus, CostFunction
from .coordinator import OptimizationCoordinator, OptimizationConfig

# Built-in LP optimizer
from .battery_optimizer import BatteryOptimizer, OptimizerResult

# Schedule data models
from .schedule_reader import OptimizationSchedule, ScheduleAction

# EV smart charging coordination
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode, EVChargingState, EVStatus

__all__ = [
    # Core optimization
    "LoadEstimator",
    "SolcastForecaster",
    "ScheduleExecutor",
    "OptimizationCoordinator",
    "OptimizationConfig",
    # Built-in optimizer
    "BatteryOptimizer",
    "OptimizerResult",
    # Executor types
    "BatteryAction",
    "ExecutionStatus",
    "CostFunction",
    # Schedule data models
    "OptimizationSchedule",
    "ScheduleAction",
    # EV smart charging
    "EVCoordinator",
    "EVConfig",
    "EVChargingMode",
    "EVChargingState",
    "EVStatus",
]
