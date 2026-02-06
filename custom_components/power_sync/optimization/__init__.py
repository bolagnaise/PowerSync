"""
Optimization package for PowerSync battery scheduling.

This module provides:
- BatteryOptimizer: Built-in LP optimizer (scipy) with greedy fallback
- ForecastBridge: Creates forecast sensors for dashboard visibility
- OptimizerConfigurator: Deprecated (was HAEO auto-configuration)
- ScheduleReader: Deprecated stub (was HAEO sensor reading)
- OptimizationCoordinator: Main coordinator for optimization
- ScheduleExecutor: Executes battery commands
- LoadEstimator, SolcastForecaster: Data sources for optimizer
- EVCoordinator: Smart EV charging coordination
"""
from .load_estimator import LoadEstimator, SolcastForecaster, HAFOForecaster
from .executor import ScheduleExecutor, BatteryAction, ExecutionStatus, CostFunction
from .coordinator import OptimizationCoordinator, OptimizationConfig

# Built-in LP optimizer
from .battery_optimizer import BatteryOptimizer, OptimizerResult

# Dashboard sensors
from .forecast_bridge import ForecastBridge, ForecastSensor

# Legacy / deprecated (kept for backward compatibility)
from .optimizer_configurator import OptimizerConfigurator
from .schedule_reader import ScheduleReader, OptimizationSchedule, ScheduleAction

# EV smart charging coordination
from .ev_coordinator import EVCoordinator, EVConfig, EVChargingMode, EVChargingState, EVStatus

__all__ = [
    # Core optimization
    "LoadEstimator",
    "SolcastForecaster",
    "HAFOForecaster",
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
    # Dashboard sensors
    "ForecastBridge",
    "ForecastSensor",
    # Legacy / deprecated
    "OptimizerConfigurator",
    "ScheduleReader",
    "OptimizationSchedule",
    "ScheduleAction",
    # EV smart charging
    "EVCoordinator",
    "EVConfig",
    "EVChargingMode",
    "EVChargingState",
    "EVStatus",
]
