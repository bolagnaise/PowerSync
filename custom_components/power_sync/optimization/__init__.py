"""Optimization package for PowerSync ML-based battery scheduling."""
from .engine import BatteryOptimizer, OptimizationResult, OptimizationConfig, CostFunction
from .load_estimator import LoadEstimator, SolcastForecaster
from .executor import ScheduleExecutor
from .coordinator import OptimizationCoordinator

# Future enhancements
from .ml_load_forecaster import (
    MLLoadEstimator,
    WeatherAdjustedForecaster,
    WeatherFeatures,
    LoadFeatures,
)
from .ev_integration import (
    EVOptimizer,
    EVConfig,
    EVChargingSchedule,
    EVChargingPriority,
    integrate_ev_with_home_battery,
)
from .multi_battery import (
    MultiBatteryOptimizer,
    MultiBatteryResult,
    BatteryConfig,
    BatterySystemType,
)
from .grid_services import (
    GridServicesManager,
    VPPAwareOptimizer,
    VPPConfig,
    VPPProgram,
    GridEvent,
    GridEventType,
    GridEventResponse,
)

__all__ = [
    # Core optimization
    "BatteryOptimizer",
    "OptimizationResult",
    "OptimizationConfig",
    "CostFunction",
    "LoadEstimator",
    "SolcastForecaster",
    "ScheduleExecutor",
    "OptimizationCoordinator",
    # ML Load Forecasting
    "MLLoadEstimator",
    "WeatherAdjustedForecaster",
    "WeatherFeatures",
    "LoadFeatures",
    # EV Integration
    "EVOptimizer",
    "EVConfig",
    "EVChargingSchedule",
    "EVChargingPriority",
    "integrate_ev_with_home_battery",
    # Multi-battery
    "MultiBatteryOptimizer",
    "MultiBatteryResult",
    "BatteryConfig",
    "BatterySystemType",
    # Grid Services / VPP
    "GridServicesManager",
    "VPPAwareOptimizer",
    "VPPConfig",
    "VPPProgram",
    "GridEvent",
    "GridEventType",
    "GridEventResponse",
]
