"""
PowerSync Optimizer HTTP API Server.

Provides REST API endpoints for the PowerSync integration to request
battery optimization schedules.
"""
import logging
import os
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS

from .engine import BatteryOptimizer, OptimizationConfig, CostFunction

# Configure logging
log_level = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Create Flask app
app = Flask(__name__)
CORS(app)

# Global optimizer instance
optimizer = BatteryOptimizer()

# Configuration from environment
OPTIMIZATION_INTERVAL = int(os.environ.get("OPTIMIZATION_INTERVAL", 30))
HORIZON_HOURS = int(os.environ.get("HORIZON_HOURS", 48))
DEFAULT_COST_FUNCTION = os.environ.get("DEFAULT_COST_FUNCTION", "cost")


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "healthy",
        "optimizer_available": optimizer.is_available,
        "version": "1.0.0",
    })


@app.route("/status", methods=["GET"])
def status():
    """Get optimizer status and configuration."""
    return jsonify({
        "success": True,
        "optimizer_available": optimizer.is_available,
        "config": {
            "optimization_interval": OPTIMIZATION_INTERVAL,
            "horizon_hours": HORIZON_HOURS,
            "default_cost_function": DEFAULT_COST_FUNCTION,
        },
    })


@app.route("/optimize", methods=["POST"])
def optimize():
    """
    Run optimization with provided data.

    Request body:
    {
        "prices_import": [0.25, 0.30, ...],  # $/kWh for each interval
        "prices_export": [0.10, 0.12, ...],  # $/kWh for each interval
        "solar_forecast": [0, 0, 500, ...],  # Watts for each interval
        "load_forecast": [1000, 800, ...],   # Watts for each interval
        "battery": {
            "current_soc": 0.5,              # 0-1
            "capacity_wh": 13500,
            "max_charge_w": 5000,
            "max_discharge_w": 5000,
            "efficiency": 0.9,
            "backup_reserve": 0.2
        },
        "cost_function": "cost",  # cost|profit|self_consumption
        "interval_minutes": 30,
        "provider_config": {  # Optional
            "export_boost_enabled": false,
            "export_price_offset": 0,
            "chip_mode_enabled": false,
            ...
        }
    }
    """
    if not optimizer.is_available:
        return jsonify({
            "success": False,
            "error": "Optimizer not available (missing cvxpy/numpy)",
        }), 503

    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        # Extract required fields
        prices_import = data.get("prices_import", [])
        prices_export = data.get("prices_export", [])
        solar_forecast = data.get("solar_forecast", [])
        load_forecast = data.get("load_forecast", [])
        battery = data.get("battery", {})

        if not prices_import:
            return jsonify({"success": False, "error": "prices_import required"}), 400

        # Get battery parameters
        current_soc = battery.get("current_soc", 0.5)
        capacity_wh = battery.get("capacity_wh", 13500)
        max_charge_w = battery.get("max_charge_w", 5000)
        max_discharge_w = battery.get("max_discharge_w", 5000)
        efficiency = battery.get("efficiency", 0.9)
        backup_reserve = battery.get("backup_reserve", 0.2)

        # Get cost function
        cost_function_str = data.get("cost_function", DEFAULT_COST_FUNCTION)
        cost_function = {
            "cost": CostFunction.COST_MINIMIZATION,
            "profit": CostFunction.PROFIT_MAXIMIZATION,
            "self_consumption": CostFunction.SELF_CONSUMPTION,
        }.get(cost_function_str, CostFunction.COST_MINIMIZATION)

        # Get interval
        interval_minutes = data.get("interval_minutes", OPTIMIZATION_INTERVAL)

        # Apply provider price modifications if provided
        provider_config = data.get("provider_config", {})
        if provider_config:
            prices_import, prices_export = apply_price_modifications(
                prices_import, prices_export, provider_config, interval_minutes
            )

        # Build config
        config = OptimizationConfig(
            battery_capacity_wh=capacity_wh,
            max_charge_w=max_charge_w,
            max_discharge_w=max_discharge_w,
            charge_efficiency=efficiency,
            discharge_efficiency=efficiency,
            backup_reserve=backup_reserve,
            cost_function=cost_function,
            interval_minutes=interval_minutes,
            horizon_hours=HORIZON_HOURS,
        )

        # Ensure forecasts have enough data
        n_intervals = HORIZON_HOURS * 60 // interval_minutes

        # Pad solar forecast if needed
        if len(solar_forecast) < n_intervals:
            solar_forecast = solar_forecast + [0] * (n_intervals - len(solar_forecast))

        # Pad load forecast if needed (use average if available)
        if len(load_forecast) < n_intervals:
            avg_load = sum(load_forecast) / len(load_forecast) if load_forecast else 1000
            load_forecast = load_forecast + [avg_load] * (n_intervals - len(load_forecast))

        # Run optimization
        logger.info(f"Running optimization: {len(prices_import)} intervals, SOC={current_soc:.1%}")
        result = optimizer.optimize(
            prices_import=prices_import[:n_intervals],
            prices_export=prices_export[:n_intervals],
            solar_forecast=solar_forecast[:n_intervals],
            load_forecast=load_forecast[:n_intervals],
            initial_soc=current_soc,
            config=config,
        )

        if not result.success:
            logger.warning(f"Optimization failed: {result.status}")
            return jsonify({
                "success": False,
                "error": result.status,
            }), 500

        # Build response
        logger.info(f"Optimization succeeded: cost=${result.total_cost:.2f}, "
                   f"import={result.total_import_kwh:.1f}kWh, export={result.total_export_kwh:.1f}kWh")

        return jsonify({
            "success": True,
            "status": result.status,
            "schedule": {
                "timestamps": [t.isoformat() for t in result.timestamps],
                "charge_w": result.charge_schedule_w,
                "discharge_w": result.discharge_schedule_w,
                "grid_import_w": result.grid_import_w,
                "grid_export_w": result.grid_export_w,
                "soc_trajectory": result.soc_trajectory,
            },
            "summary": {
                "total_cost": result.total_cost,
                "total_import_kwh": result.total_import_kwh,
                "total_export_kwh": result.total_export_kwh,
                "total_charge_kwh": result.total_charge_kwh,
                "total_discharge_kwh": result.total_discharge_kwh,
                "average_import_price": result.average_import_price,
                "average_export_price": result.average_export_price,
            },
        })

    except Exception as e:
        logger.exception("Optimization error")
        return jsonify({
            "success": False,
            "error": str(e),
        }), 500


def apply_price_modifications(
    prices_import: list[float],
    prices_export: list[float],
    config: dict,
    interval_minutes: int,
) -> tuple[list[float], list[float]]:
    """Apply export boost, chip mode, etc. to prices."""
    from datetime import datetime

    now = datetime.now()
    modified_import = list(prices_import)
    modified_export = list(prices_export)

    def time_to_minutes(t: str) -> int:
        parts = t.split(":")
        return int(parts[0]) * 60 + int(parts[1])

    def is_in_window(minutes: int, start: str, end: str) -> bool:
        start_m = time_to_minutes(start)
        end_m = time_to_minutes(end)
        if start_m <= end_m:
            return start_m <= minutes < end_m
        else:
            return minutes >= start_m or minutes < end_m

    for i in range(len(prices_export)):
        interval_time = now + timedelta(minutes=i * interval_minutes)
        minutes = interval_time.hour * 60 + interval_time.minute

        # Apply export boost
        if config.get("export_boost_enabled", False):
            start = config.get("export_boost_start", "17:00")
            end = config.get("export_boost_end", "21:00")
            if is_in_window(minutes, start, end):
                offset = config.get("export_price_offset", 0) / 100  # cents to $
                min_price = config.get("export_min_price", 0) / 100
                modified_export[i] = max(modified_export[i] + offset, min_price)

        # Apply chip mode
        if config.get("chip_mode_enabled", False):
            start = config.get("chip_mode_start", "22:00")
            end = config.get("chip_mode_end", "06:00")
            threshold = config.get("chip_mode_threshold", 30) / 100  # cents to $
            if is_in_window(minutes, start, end):
                if modified_export[i] < threshold:
                    modified_export[i] = -1.0  # Penalty for export

    return modified_import, modified_export


@app.route("/ev/optimize", methods=["POST"])
def optimize_ev():
    """
    Run EV charging optimization.

    Request body:
    {
        "ev": {
            "vehicle_id": "my_ev",
            "battery_capacity_kwh": 75,
            "current_soc": 0.3,
            "target_soc": 0.8,
            "departure_time": "2024-01-15T07:00:00",
            "max_charge_kw": 11
        },
        "prices_import": [...],
        "solar_forecast": [...],
        "home_battery": {...}  # Optional - for joint optimization
    }
    """
    # TODO: Implement EV optimization
    return jsonify({
        "success": False,
        "error": "EV optimization not yet implemented",
    }), 501


@app.route("/multi-battery/optimize", methods=["POST"])
def optimize_multi_battery():
    """
    Run multi-battery optimization.

    Request body:
    {
        "batteries": [
            {"id": "pw1", "capacity_wh": 13500, "current_soc": 0.5, ...},
            {"id": "sigen", "capacity_wh": 10000, "current_soc": 0.6, ...}
        ],
        "prices_import": [...],
        "prices_export": [...],
        "solar_forecast": [...],
        "load_forecast": [...]
    }
    """
    # TODO: Implement multi-battery optimization
    return jsonify({
        "success": False,
        "error": "Multi-battery optimization not yet implemented",
    }), 501


if __name__ == "__main__":
    logger.info("Starting PowerSync Optimizer server...")
    logger.info(f"Optimizer available: {optimizer.is_available}")
    app.run(host="0.0.0.0", port=5000, debug=False)
