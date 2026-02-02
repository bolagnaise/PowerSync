"""Optimization package for PowerSync ML-based battery scheduling."""
from .engine import BatteryOptimiser, OptimizationResult, OptimizationConfig, CostFunction
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
    EVOptimiser,
    EVConfig,
    EVChargingSchedule,
    EVChargingPriority,
    integrate_ev_with_home_battery,
)
from .multi_battery import (
    MultiBatteryOptimiser,
    MultiBatteryResult,
    BatteryConfig,
    BatterySystemType,
)
from .grid_services import (
    GridServicesManager,
    VPPAwareOptimiser,
    VPPConfig,
    VPPProgram,
    GridEvent,
    GridEventType,
    GridEventResponse,
)

__all__ = [
    # Core optimization
    "BatteryOptimiser",
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
    "EVOptimiser",
    "EVConfig",
    "EVChargingSchedule",
    "EVChargingPriority",
    "integrate_ev_with_home_battery",
    # Multi-battery
    "MultiBatteryOptimiser",
    "MultiBatteryResult",
    "BatteryConfig",
    "BatterySystemType",
    # Grid Services / VPP
    "GridServicesManager",
    "VPPAwareOptimiser",
    "VPPConfig",
    "VPPProgram",
    "GridEvent",
    "GridEventType",
    "GridEventResponse",
]
