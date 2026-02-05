"""
Optimization package for PowerSync battery scheduling.

This module provides:
- ForecastBridge: Creates forecast sensors for optimizer consumption
- OptimizerConfigurator: Auto-configures external optimizer network
- ScheduleReader: Reads optimizer output sensors
- OptimizationCoordinator: Main coordinator for optimization
- ScheduleExecutor: Executes battery commands
- LoadEstimator, SolcastForecaster: Data sources for optimizer
- EVCoordinator: Smart EV charging coordination (post-optimizer)

External optimizer integration performs the actual LP optimization.
"""
from .load_estimator import LoadEstimator, SolcastForecaster, HAFOForecaster
from .executor import ScheduleExecutor, BatteryAction, ExecutionStatus, CostFunction
from .coordinator import OptimizationCoordinator, OptimizationConfig

# Optimizer integration components
from .forecast_bridge import ForecastBridge, ForecastSensor
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
    # Executor types
    "BatteryAction",
    "ExecutionStatus",
    "CostFunction",
    # Optimizer integration
    "ForecastBridge",
    "ForecastSensor",
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
