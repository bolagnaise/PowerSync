"""
EV Charging Planner - Smart scheduling with forecasting.

Plans optimal charging windows based on:
- Solar forecast (Solcast integration)
- Electricity prices (Amber/Flow Power)
- Vehicle departure times
- Current SoC and target SoC
- Historical load patterns
"""

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, List, Dict, Any, Tuple
from enum import Enum

_LOGGER = logging.getLogger(__name__)


class ChargingPriority(Enum):
    """Priority for charging source selection."""
    SOLAR_ONLY = "solar_only"  # Only charge from solar surplus
    SOLAR_PREFERRED = "solar_preferred"  # Prefer solar, allow offpeak grid
    COST_OPTIMIZED = "cost_optimized"  # Minimize cost (solar > cheap grid > expensive grid)
    TIME_CRITICAL = "time_critical"  # Must reach target by deadline, any source


@dataclass
class SurplusForecast:
    """Hourly solar surplus forecast."""
    hour: str  # ISO format
    solar_kw: float
    load_kw: float
    surplus_kw: float
    confidence: float  # 0-1


@dataclass
class PriceForecast:
    """Hourly electricity price forecast."""
    hour: str  # ISO format
    import_cents: float
    export_cents: float
    period: str  # 'offpeak', 'shoulder', 'peak'


@dataclass
class PlannedChargingWindow:
    """A planned charging window."""
    start_time: str  # ISO format
    end_time: str
    source: str  # 'solar_surplus', 'grid_offpeak', 'grid_peak'
    estimated_power_kw: float
    estimated_energy_kwh: float
    price_cents_kwh: float
    reason: str  # 'solar_forecast', 'offpeak_rate', 'target_deadline'


@dataclass
class ChargingPlan:
    """Complete charging plan for a vehicle."""
    vehicle_id: str
    current_soc: int
    target_soc: int
    target_time: Optional[str]  # ISO format
    energy_needed_kwh: float

    # Planned windows
    windows: List[PlannedChargingWindow] = field(default_factory=list)

    # Estimates
    estimated_solar_kwh: float = 0.0
    estimated_grid_kwh: float = 0.0
    estimated_cost_cents: float = 0.0
    confidence: float = 0.0  # 0-1, based on forecast reliability

    # Status
    can_meet_target: bool = True
    warning: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API response."""
        return {
            "vehicle_id": self.vehicle_id,
            "current_soc": self.current_soc,
            "target_soc": self.target_soc,
            "target_time": self.target_time,
            "energy_needed_kwh": round(self.energy_needed_kwh, 2),
            "planned_windows": [
                {
                    "start_time": w.start_time,
                    "end_time": w.end_time,
                    "source": w.source,
                    "estimated_power_kw": round(w.estimated_power_kw, 2),
                    "estimated_energy_kwh": round(w.estimated_energy_kwh, 2),
                    "price_cents_kwh": round(w.price_cents_kwh, 1),
                    "reason": w.reason,
                }
                for w in self.windows
            ],
            "estimated_solar_kwh": round(self.estimated_solar_kwh, 2),
            "estimated_grid_kwh": round(self.estimated_grid_kwh, 2),
            "estimated_cost_cents": round(self.estimated_cost_cents, 0),
            "confidence": round(self.confidence, 2),
            "can_meet_target": self.can_meet_target,
            "warning": self.warning,
        }


class LoadProfileEstimator:
    """Estimates household load based on historical patterns."""

    # Default load profile (kW) by hour for weekday
    DEFAULT_WEEKDAY_PROFILE = [
        0.4, 0.3, 0.3, 0.3, 0.3, 0.4,  # 00:00-05:59 (night, low)
        0.8, 1.2, 1.0, 0.6, 0.5, 0.5,  # 06:00-11:59 (morning peak, then low)
        0.5, 0.5, 0.6, 0.7, 0.8, 1.5,  # 12:00-17:59 (afternoon, evening peak starts)
        2.0, 1.8, 1.2, 0.8, 0.6, 0.5,  # 18:00-23:59 (evening peak, then declining)
    ]

    # Weekend profile (slightly different pattern)
    DEFAULT_WEEKEND_PROFILE = [
        0.4, 0.3, 0.3, 0.3, 0.3, 0.3,  # 00:00-05:59 (night)
        0.5, 0.7, 1.0, 1.2, 1.0, 0.8,  # 06:00-11:59 (later wake, higher morning)
        0.7, 0.6, 0.6, 0.7, 0.8, 1.2,  # 12:00-17:59 (more activity)
        1.5, 1.4, 1.0, 0.8, 0.6, 0.5,  # 18:00-23:59 (earlier evening decline)
    ]

    def __init__(self, hass):
        """Initialize the estimator.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass
        self._load_history: Dict[str, List[float]] = {}
        self._last_history_update: Optional[datetime] = None

    async def get_typical_load_profile(self, day_type: str = "weekday") -> List[float]:
        """
        Get 24-hour load profile in kW based on historical data.

        Args:
            day_type: "weekday" or "weekend"

        Returns:
            List of 24 hourly load values in kW
        """
        # Try to get from history first
        if self._load_history.get(day_type):
            return self._load_history[day_type]

        # Fall back to defaults
        if day_type == "weekend":
            return self.DEFAULT_WEEKEND_PROFILE.copy()
        return self.DEFAULT_WEEKDAY_PROFILE.copy()

    async def update_from_history(self, days: int = 14) -> None:
        """
        Update load profiles from Home Assistant history.

        Args:
            days: Number of days of history to analyze
        """
        try:
            # Check if we've updated recently
            if self._last_history_update:
                if (datetime.now() - self._last_history_update).total_seconds() < 3600:
                    return  # Updated within last hour

            # Find load power sensor
            load_entity = None
            for entity_id in self.hass.states.async_entity_ids("sensor"):
                if "load_power" in entity_id.lower() or "home_power" in entity_id.lower():
                    load_entity = entity_id
                    break

            if not load_entity:
                _LOGGER.debug("No load power entity found for profile estimation")
                return

            # Query history
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            start_time = datetime.now() - timedelta(days=days)
            end_time = datetime.now()

            recorder = get_instance(self.hass)
            if not recorder:
                return

            # Get historical states
            history = await recorder.async_add_executor_job(
                get_significant_states,
                self.hass,
                start_time,
                end_time,
                [load_entity],
            )

            if not history or load_entity not in history:
                return

            # Group by hour and day type
            weekday_hours: Dict[int, List[float]] = {h: [] for h in range(24)}
            weekend_hours: Dict[int, List[float]] = {h: [] for h in range(24)}

            for state in history[load_entity]:
                if state.state in ("unknown", "unavailable"):
                    continue
                try:
                    power_w = float(state.state)
                    power_kw = power_w / 1000
                    hour = state.last_updated.hour
                    is_weekend = state.last_updated.weekday() >= 5

                    if is_weekend:
                        weekend_hours[hour].append(power_kw)
                    else:
                        weekday_hours[hour].append(power_kw)
                except (ValueError, TypeError):
                    continue

            # Calculate median for each hour
            weekday_profile = []
            weekend_profile = []

            for hour in range(24):
                if weekday_hours[hour]:
                    weekday_profile.append(statistics.median(weekday_hours[hour]))
                else:
                    weekday_profile.append(self.DEFAULT_WEEKDAY_PROFILE[hour])

                if weekend_hours[hour]:
                    weekend_profile.append(statistics.median(weekend_hours[hour]))
                else:
                    weekend_profile.append(self.DEFAULT_WEEKEND_PROFILE[hour])

            self._load_history["weekday"] = weekday_profile
            self._load_history["weekend"] = weekend_profile
            self._last_history_update = datetime.now()

            _LOGGER.info(f"Updated load profiles from {days} days of history")

        except Exception as e:
            _LOGGER.debug(f"Could not update load profiles from history: {e}")

    def estimate_load_at_hour(self, target_hour: datetime) -> Tuple[float, float]:
        """
        Estimate load at a specific hour.

        Args:
            target_hour: The datetime to estimate for

        Returns:
            Tuple of (estimated_load_kw, confidence)
        """
        is_weekend = target_hour.weekday() >= 5
        day_type = "weekend" if is_weekend else "weekday"
        hour = target_hour.hour

        if day_type in self._load_history:
            profile = self._load_history[day_type]
            confidence = 0.8  # Higher confidence with historical data
        else:
            profile = self.DEFAULT_WEEKEND_PROFILE if is_weekend else self.DEFAULT_WEEKDAY_PROFILE
            confidence = 0.5  # Lower confidence with defaults

        return profile[hour], confidence


class SolarForecaster:
    """Gets solar production forecast from Solcast or estimates."""

    def __init__(self, hass):
        """Initialize the forecaster.

        Args:
            hass: Home Assistant instance
        """
        self.hass = hass

    async def get_solar_forecast(self, hours: int = 24) -> List[Dict[str, Any]]:
        """
        Get hourly solar forecast.

        Tries Solcast integration first, falls back to simple estimation.

        Args:
            hours: Number of hours to forecast

        Returns:
            List of dicts with hour, pv_estimate_kw, confidence
        """
        # Try Solcast integration
        solcast_forecast = await self._get_solcast_forecast(hours)
        if solcast_forecast:
            return solcast_forecast

        # Fall back to simple estimation
        return await self._estimate_solar(hours)

    async def _get_solcast_forecast(self, hours: int) -> Optional[List[Dict[str, Any]]]:
        """Get forecast from Solcast integration if available."""
        try:
            # Look for Solcast sensors - try multiple patterns
            solcast_entity = None
            solcast_patterns = ["solcast_pv_forecast", "solcast_forecast", "solcast"]

            for entity_id in self.hass.states.async_entity_ids("sensor"):
                entity_lower = entity_id.lower()
                for pattern in solcast_patterns:
                    if pattern in entity_lower and "forecast" in entity_lower:
                        solcast_entity = entity_id
                        _LOGGER.debug(f"Found Solcast entity: {entity_id}")
                        break
                if solcast_entity:
                    break

            if not solcast_entity:
                _LOGGER.debug("No Solcast forecast entity found")
                return None

            state = self.hass.states.get(solcast_entity)
            if not state or not state.attributes:
                _LOGGER.debug(f"Solcast entity {solcast_entity} has no state or attributes")
                return None

            # Solcast stores forecast in attributes - try multiple attribute names
            forecasts = state.attributes.get("forecasts", [])
            if not forecasts:
                forecasts = state.attributes.get("detailedForecast", [])
            if not forecasts:
                forecasts = state.attributes.get("forecast_today", [])
            if not forecasts:
                forecasts = state.attributes.get("detailed_forecast", [])

            if not forecasts:
                return None

            result = []
            now = datetime.now()

            # Solcast provides 30-minute intervals, so we need 2x entries for hourly data
            # Aggregate into hourly buckets
            hourly_data = {}

            for entry in forecasts[:hours * 2]:  # Get 2x entries for 30-min intervals
                # Solcast format varies by integration version
                period_end = entry.get("period_end") or entry.get("period")
                pv_estimate = entry.get("pv_estimate") or entry.get("pv_estimate10") or 0

                if isinstance(period_end, str):
                    try:
                        period_dt = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
                    except ValueError:
                        continue
                else:
                    period_dt = period_end

                # Round down to hour for aggregation
                hour_key = period_dt.replace(minute=0, second=0, microsecond=0)

                if hour_key not in hourly_data:
                    hourly_data[hour_key] = {"total_kw": 0, "count": 0}

                # pv_estimate is average kW during 30-min period
                # Sum the averages, we'll divide by count later
                hourly_data[hour_key]["total_kw"] += float(pv_estimate)
                hourly_data[hour_key]["count"] += 1

            # Convert to hourly averages
            for hour_dt, data in sorted(hourly_data.items())[:hours]:
                avg_kw = data["total_kw"] / data["count"] if data["count"] > 0 else 0
                result.append({
                    "hour": hour_dt.isoformat(),
                    "pv_estimate_kw": avg_kw,
                    "confidence": 0.8,  # Solcast is generally reliable
                })

            _LOGGER.debug(f"Got {len(result)} hours of Solcast forecast (aggregated from 30-min intervals)")
            return result if result else None

        except Exception as e:
            _LOGGER.debug(f"Could not get Solcast forecast: {e}")
            return None

    async def _estimate_solar(self, hours: int) -> List[Dict[str, Any]]:
        """
        Simple solar estimation based on time of day.

        Uses a bell curve centered on solar noon with seasonal adjustment.
        """
        result = []
        now = datetime.now()

        # Get system size from current peak or estimate
        system_size_kw = await self._estimate_system_size()

        for h in range(hours):
            hour_dt = now + timedelta(hours=h)
            hour_of_day = hour_dt.hour

            # Simple bell curve for solar production
            # Peak around 12:00-13:00
            if 6 <= hour_of_day <= 18:
                # Normalize hour to 0-1 (6am = 0, 12pm = 0.5, 6pm = 1)
                normalized = (hour_of_day - 6) / 12
                # Bell curve: sin for smooth rise and fall
                import math
                production_factor = math.sin(normalized * math.pi)

                # Seasonal adjustment (simplified)
                month = hour_dt.month
                if month in (12, 1, 2):  # Summer in Australia
                    seasonal_factor = 1.0
                elif month in (6, 7, 8):  # Winter
                    seasonal_factor = 0.5
                else:  # Spring/Autumn
                    seasonal_factor = 0.75

                pv_estimate = system_size_kw * production_factor * seasonal_factor
            else:
                pv_estimate = 0

            result.append({
                "hour": hour_dt.isoformat(),
                "pv_estimate_kw": round(pv_estimate, 2),
                "confidence": 0.4,  # Low confidence for estimates
            })

        return result

    async def _estimate_system_size(self) -> float:
        """Estimate solar system size from current or peak production."""
        try:
            # Look for solar power sensor
            for entity_id in self.hass.states.async_entity_ids("sensor"):
                if "solar" in entity_id.lower() and "power" in entity_id.lower():
                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unknown", "unavailable"):
                        try:
                            current_power_w = float(state.state)
                            # Estimate system size as ~1.5x current production
                            # (assumes we're not at peak)
                            return max(5.0, current_power_w / 1000 * 1.5)
                        except (ValueError, TypeError):
                            pass

            # Default to 6.6kW (common Australian system size)
            return 6.6

        except Exception:
            return 6.6


class SurplusForecaster:
    """Combines solar forecast with load estimation for surplus prediction."""

    def __init__(self, hass):
        """Initialize the forecaster."""
        self.hass = hass
        self.solar_forecaster = SolarForecaster(hass)
        self.load_estimator = LoadProfileEstimator(hass)

    async def forecast_surplus(
        self,
        hours: int = 24,
        battery_reserve_kw: float = 1.0,
    ) -> List[SurplusForecast]:
        """
        Forecast available solar surplus for each hour.

        Args:
            hours: Number of hours to forecast
            battery_reserve_kw: Power to reserve for battery charging

        Returns:
            List of SurplusForecast objects
        """
        # Update load profiles if needed
        await self.load_estimator.update_from_history()

        # Get solar forecast
        solar_forecast = await self.solar_forecaster.get_solar_forecast(hours)

        # Build surplus forecast
        forecasts = []
        now = datetime.now()

        for i, solar_data in enumerate(solar_forecast):
            hour_dt = now + timedelta(hours=i)

            # Get solar estimate
            pv_kw = solar_data.get("pv_estimate_kw", 0)
            solar_confidence = solar_data.get("confidence", 0.5)

            # Get load estimate
            load_kw, load_confidence = self.load_estimator.estimate_load_at_hour(hour_dt)

            # Calculate surplus (available for EV after battery reserve)
            surplus_kw = max(0, pv_kw - load_kw - battery_reserve_kw)

            # Combined confidence
            confidence = (solar_confidence + load_confidence) / 2

            forecasts.append(SurplusForecast(
                hour=hour_dt.isoformat(),
                solar_kw=pv_kw,
                load_kw=load_kw,
                surplus_kw=round(surplus_kw, 2),
                confidence=round(confidence, 2),
            ))

        return forecasts


class PriceForecaster:
    """Gets electricity price forecasts."""

    def __init__(self, hass, config_entry):
        """Initialize the forecaster."""
        self.hass = hass
        self.config_entry = config_entry

    async def get_price_forecast(self, hours: int = 24) -> List[PriceForecast]:
        """
        Get hourly price forecast (provider-aware).

        For Amber/Flow Power: uses Amber API forecast
        For Globird: uses Tesla tariff TOU schedule
        Falls back to generic TOU estimation.

        Args:
            hours: Number of hours to forecast

        Returns:
            List of PriceForecast objects
        """
        from ..const import CONF_ELECTRICITY_PROVIDER

        # Get electricity provider
        electricity_provider = self.config_entry.options.get(
            CONF_ELECTRICITY_PROVIDER,
            self.config_entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
        )

        if electricity_provider in ("amber", "flow_power"):
            # Try Amber forecast
            amber_forecast = await self._get_amber_forecast(hours)
            if amber_forecast:
                return amber_forecast

        elif electricity_provider in ("globird", "aemo_vpp"):
            # Try Tesla tariff forecast
            tariff_forecast = await self._get_tariff_forecast(hours)
            if tariff_forecast:
                return tariff_forecast

        # Fall back to TOU estimation
        return await self._estimate_tou_prices(hours)

    async def _get_amber_forecast(self, hours: int) -> Optional[List[PriceForecast]]:
        """Get forecast from Amber coordinator data."""
        try:
            from ..const import DOMAIN, CONF_AMBER_API_TOKEN

            # Check if Amber is configured
            amber_token = self.config_entry.data.get(CONF_AMBER_API_TOKEN)
            if not amber_token:
                return None

            # Get forecast from amber_coordinator
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            amber_coordinator = entry_data.get("amber_coordinator")

            if not amber_coordinator or not amber_coordinator.data:
                _LOGGER.debug("No Amber coordinator data available")
                return None

            # Get forecast data from coordinator (Amber API format)
            forecast_data = amber_coordinator.data.get("forecast", [])
            if not forecast_data:
                _LOGGER.debug("No forecast data in Amber coordinator")
                return None

            # Parse Amber forecast into our format
            # Group by hour and separate import/export prices
            hourly_prices = {}
            now = datetime.now()

            for price_item in forecast_data:
                # Parse the NEM time
                nem_time = price_item.get("nemTime") or price_item.get("startTime")
                if not nem_time:
                    continue

                try:
                    # Parse ISO format time
                    if "T" in nem_time:
                        hour_dt = datetime.fromisoformat(nem_time.replace("Z", "+00:00"))
                        # Convert to local time
                        hour_dt = hour_dt.replace(tzinfo=None)
                    else:
                        continue

                    hour_key = hour_dt.strftime("%Y-%m-%dT%H:00")

                    if hour_key not in hourly_prices:
                        hourly_prices[hour_key] = {"import": None, "export": None, "hour_dt": hour_dt}

                    channel = price_item.get("channelType", "general")
                    per_kwh = price_item.get("perKwh", 0)

                    if channel == "general":
                        # Use first price of the hour (or average if multiple)
                        if hourly_prices[hour_key]["import"] is None:
                            hourly_prices[hour_key]["import"] = per_kwh
                    elif channel == "feedIn":
                        if hourly_prices[hour_key]["export"] is None:
                            hourly_prices[hour_key]["export"] = per_kwh

                except Exception as e:
                    _LOGGER.debug(f"Error parsing forecast item: {e}")
                    continue

            # Convert to PriceForecast list, sorted by time
            forecasts = []
            sorted_hours = sorted(hourly_prices.items(), key=lambda x: x[1]["hour_dt"])

            for hour_key, prices in sorted_hours[:hours]:
                import_cents = prices["import"] if prices["import"] is not None else 30
                export_cents = prices["export"] if prices["export"] is not None else 8
                hour_dt = prices["hour_dt"]

                # Determine period based on price
                if import_cents < 15:
                    period = "offpeak"
                elif import_cents > 35:
                    period = "peak"
                else:
                    period = "shoulder"

                forecasts.append(PriceForecast(
                    hour=hour_dt.isoformat(),
                    import_cents=import_cents,
                    export_cents=export_cents,
                    period=period,
                ))

            if forecasts:
                _LOGGER.info(f"Got {len(forecasts)} hours of Amber price forecast")
                # Log a few sample prices for debugging
                if len(forecasts) >= 3:
                    _LOGGER.debug(
                        f"Sample prices: now={forecasts[0].import_cents:.1f}c, "
                        f"+1h={forecasts[1].import_cents:.1f}c, +2h={forecasts[2].import_cents:.1f}c"
                    )

            return forecasts if forecasts else None

        except Exception as e:
            _LOGGER.debug(f"Could not get Amber forecast: {e}")
            return None

    async def _get_tariff_forecast(self, hours: int) -> Optional[List[PriceForecast]]:
        """Get forecast from Tesla tariff schedule (for Globird users)."""
        try:
            from ..const import DOMAIN

            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            tariff_schedule = entry_data.get("tariff_schedule", {})

            if not tariff_schedule:
                return None

            # Get current prices from tariff
            buy_price_cents = tariff_schedule.get("buy_price", 30)
            sell_price_cents = tariff_schedule.get("sell_price", 0)
            buy_rates = tariff_schedule.get("buy_rates", {})
            sell_rates = tariff_schedule.get("sell_rates", {})

            forecasts = []
            now = datetime.now()

            for h in range(hours):
                hour_dt = now + timedelta(hours=h)
                hour = hour_dt.hour
                is_weekend = hour_dt.weekday() >= 5

                # Determine TOU period based on typical patterns
                if is_weekend:
                    period_type = "OFF_PEAK"
                elif 7 <= hour < 9 or 17 <= hour < 21:
                    period_type = "ON_PEAK"
                elif 21 <= hour or hour < 7:
                    period_type = "OFF_PEAK"
                else:
                    period_type = "SHOULDER"

                # Get rate for this period from tariff
                import_rate = buy_rates.get(period_type, buy_rates.get("ALL", buy_price_cents / 100))
                export_rate = sell_rates.get(period_type, sell_rates.get("ALL", sell_price_cents / 100))

                # Convert to cents if in dollars
                if import_rate < 1:  # Likely in $/kWh
                    import_cents = import_rate * 100
                else:
                    import_cents = import_rate

                if export_rate < 1:
                    export_cents = export_rate * 100
                else:
                    export_cents = export_rate

                # Determine display period
                if import_cents < 20:
                    period = "offpeak"
                elif import_cents > 35:
                    period = "peak"
                else:
                    period = "shoulder"

                forecasts.append(PriceForecast(
                    hour=hour_dt.isoformat(),
                    import_cents=import_cents,
                    export_cents=export_cents,
                    period=period,
                ))

            return forecasts

        except Exception as e:
            _LOGGER.debug(f"Could not get tariff forecast: {e}")
            return None

    async def _estimate_tou_prices(self, hours: int) -> List[PriceForecast]:
        """
        Estimate prices based on typical TOU tariff structure.

        Uses common Australian TOU patterns.
        """
        forecasts = []
        now = datetime.now()

        # Typical TOU rates (cents/kWh)
        OFFPEAK_RATE = 15
        SHOULDER_RATE = 25
        PEAK_RATE = 45
        EXPORT_RATE = 8

        for h in range(hours):
            hour_dt = now + timedelta(hours=h)
            hour = hour_dt.hour
            is_weekend = hour_dt.weekday() >= 5

            # Determine period and rate
            if is_weekend:
                # Weekend: shoulder all day
                period = "shoulder"
                import_cents = SHOULDER_RATE
            elif 7 <= hour < 9 or 17 <= hour < 21:
                # Weekday peak
                period = "peak"
                import_cents = PEAK_RATE
            elif 21 <= hour or hour < 7:
                # Offpeak (night)
                period = "offpeak"
                import_cents = OFFPEAK_RATE
            else:
                # Shoulder (daytime)
                period = "shoulder"
                import_cents = SHOULDER_RATE

            forecasts.append(PriceForecast(
                hour=hour_dt.isoformat(),
                import_cents=import_cents,
                export_cents=EXPORT_RATE,
                period=period,
            ))

        return forecasts


class ChargingPlanner:
    """
    Plans optimal EV charging windows based on forecasts.

    Considers:
    - Solar surplus forecast
    - Electricity prices
    - Vehicle departure time
    - Battery capacity and efficiency
    """

    # Typical EV battery sizes (kWh)
    BATTERY_SIZES = {
        "tesla_model_3_sr": 57.5,
        "tesla_model_3_lr": 82,
        "tesla_model_y_sr": 57.5,
        "tesla_model_y_lr": 82,
        "default": 60,
    }

    # Charging efficiency (AC to DC)
    CHARGING_EFFICIENCY = 0.9

    def __init__(self, hass, config_entry):
        """Initialize the planner."""
        self.hass = hass
        self.config_entry = config_entry
        self.surplus_forecaster = SurplusForecaster(hass)
        self.price_forecaster = PriceForecaster(hass, config_entry)

    async def plan_charging(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        charger_power_kw: float = 7.0,
        battery_capacity_kwh: float = 60.0,
        priority: ChargingPriority = ChargingPriority.SOLAR_PREFERRED,
    ) -> ChargingPlan:
        """
        Create optimal charging plan.

        Args:
            vehicle_id: Vehicle identifier
            current_soc: Current state of charge (%)
            target_soc: Target state of charge (%)
            target_time: Optional deadline (must be charged by this time)
            charger_power_kw: Maximum charger power
            battery_capacity_kwh: Vehicle battery capacity
            priority: Charging priority strategy

        Returns:
            ChargingPlan with optimal windows
        """
        # Calculate energy needed
        soc_delta = target_soc - current_soc
        if soc_delta <= 0:
            return ChargingPlan(
                vehicle_id=vehicle_id,
                current_soc=current_soc,
                target_soc=target_soc,
                target_time=target_time.isoformat() if target_time else None,
                energy_needed_kwh=0,
                can_meet_target=True,
            )

        energy_needed_kwh = (soc_delta / 100) * battery_capacity_kwh / self.CHARGING_EFFICIENCY

        # Calculate hours until deadline
        if target_time:
            # Ensure both datetimes are comparable (handle timezone-aware vs naive)
            now = datetime.now()
            if target_time.tzinfo is not None and now.tzinfo is None:
                # target_time is timezone-aware, make now naive by removing tz or use target's timezone
                try:
                    now = datetime.now(target_time.tzinfo)
                except Exception:
                    # Fallback: strip timezone from target_time
                    target_time = target_time.replace(tzinfo=None)
            elif target_time.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            hours_available = max(1, int((target_time - now).total_seconds() / 3600))
        else:
            hours_available = 24

        # Get forecasts
        surplus_forecast = await self.surplus_forecaster.forecast_surplus(hours_available)
        price_forecast = await self.price_forecaster.get_price_forecast(hours_available)

        # Create plan based on priority
        if priority == ChargingPriority.SOLAR_ONLY:
            plan = await self._plan_solar_only(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast,
            )
        elif priority == ChargingPriority.SOLAR_PREFERRED:
            plan = await self._plan_solar_preferred(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
            )
        elif priority == ChargingPriority.COST_OPTIMIZED:
            plan = await self._plan_cost_optimized(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
            )
        else:  # TIME_CRITICAL
            plan = await self._plan_time_critical(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
            )

        return plan

    async def _plan_solar_only(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        energy_needed_kwh: float,
        charger_power_kw: float,
        surplus_forecast: List[SurplusForecast],
    ) -> ChargingPlan:
        """Plan charging using only solar surplus."""
        windows = []
        energy_allocated = 0
        total_confidence = 0

        for forecast in surplus_forecast:
            if energy_allocated >= energy_needed_kwh:
                break

            if forecast.surplus_kw >= 1.0:  # Minimum 1kW to charge
                # Calculate how much we can charge this hour
                available_power = min(forecast.surplus_kw, charger_power_kw)
                energy_this_hour = available_power  # kWh (1 hour)

                # Don't over-allocate
                energy_this_hour = min(energy_this_hour, energy_needed_kwh - energy_allocated)

                hour_dt = datetime.fromisoformat(forecast.hour)
                end_dt = hour_dt + timedelta(hours=1)

                windows.append(PlannedChargingWindow(
                    start_time=forecast.hour,
                    end_time=end_dt.isoformat(),
                    source="solar_surplus",
                    estimated_power_kw=available_power,
                    estimated_energy_kwh=energy_this_hour,
                    price_cents_kwh=0,  # Solar is free
                    reason="solar_forecast",
                ))

                energy_allocated += energy_this_hour
                total_confidence += forecast.confidence

        # Calculate averages
        avg_confidence = total_confidence / len(windows) if windows else 0
        can_meet = energy_allocated >= energy_needed_kwh * 0.9  # 90% is acceptable

        plan = ChargingPlan(
            vehicle_id=vehicle_id,
            current_soc=current_soc,
            target_soc=target_soc,
            target_time=target_time.isoformat() if target_time else None,
            energy_needed_kwh=energy_needed_kwh,
            windows=windows,
            estimated_solar_kwh=energy_allocated,
            estimated_grid_kwh=0,
            estimated_cost_cents=0,
            confidence=avg_confidence,
            can_meet_target=can_meet,
            warning=None if can_meet else f"Solar only can provide {energy_allocated:.1f} of {energy_needed_kwh:.1f} kWh needed",
        )

        return plan

    async def _plan_solar_preferred(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        energy_needed_kwh: float,
        charger_power_kw: float,
        surplus_forecast: List[SurplusForecast],
        price_forecast: List[PriceForecast],
    ) -> ChargingPlan:
        """Plan charging preferring solar, falling back to offpeak grid."""
        windows = []
        solar_energy = 0
        grid_energy = 0
        total_cost = 0
        total_confidence = 0

        # First pass: allocate solar
        for forecast in surplus_forecast:
            if solar_energy + grid_energy >= energy_needed_kwh:
                break

            if forecast.surplus_kw >= 1.0:
                available_power = min(forecast.surplus_kw, charger_power_kw)
                energy_this_hour = min(available_power, energy_needed_kwh - solar_energy - grid_energy)

                hour_dt = datetime.fromisoformat(forecast.hour)
                end_dt = hour_dt + timedelta(hours=1)

                windows.append(PlannedChargingWindow(
                    start_time=forecast.hour,
                    end_time=end_dt.isoformat(),
                    source="solar_surplus",
                    estimated_power_kw=available_power,
                    estimated_energy_kwh=energy_this_hour,
                    price_cents_kwh=0,
                    reason="solar_forecast",
                ))

                solar_energy += energy_this_hour
                total_confidence += forecast.confidence

        # Second pass: fill with offpeak grid if needed
        remaining_energy = energy_needed_kwh - solar_energy
        if remaining_energy > 0:
            # Find offpeak hours
            offpeak_hours = [
                p for p in price_forecast
                if p.period == "offpeak"
            ]

            for price_data in offpeak_hours:
                if grid_energy >= remaining_energy:
                    break

                # Check if this hour is already covered by solar
                already_covered = any(
                    w.start_time == price_data.hour for w in windows
                )
                if already_covered:
                    continue

                energy_this_hour = min(charger_power_kw, remaining_energy - grid_energy)

                hour_dt = datetime.fromisoformat(price_data.hour)
                end_dt = hour_dt + timedelta(hours=1)

                windows.append(PlannedChargingWindow(
                    start_time=price_data.hour,
                    end_time=end_dt.isoformat(),
                    source="grid_offpeak",
                    estimated_power_kw=charger_power_kw,
                    estimated_energy_kwh=energy_this_hour,
                    price_cents_kwh=price_data.import_cents,
                    reason="offpeak_rate",
                ))

                grid_energy += energy_this_hour
                total_cost += energy_this_hour * price_data.import_cents
                total_confidence += 0.9  # Grid is reliable

        # Sort windows by time
        windows.sort(key=lambda w: w.start_time)

        # Check if we can meet target
        total_energy = solar_energy + grid_energy
        can_meet = total_energy >= energy_needed_kwh * 0.9

        plan = ChargingPlan(
            vehicle_id=vehicle_id,
            current_soc=current_soc,
            target_soc=target_soc,
            target_time=target_time.isoformat() if target_time else None,
            energy_needed_kwh=energy_needed_kwh,
            windows=windows,
            estimated_solar_kwh=solar_energy,
            estimated_grid_kwh=grid_energy,
            estimated_cost_cents=total_cost,
            confidence=total_confidence / len(windows) if windows else 0,
            can_meet_target=can_meet,
        )

        return plan

    async def _plan_cost_optimized(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        energy_needed_kwh: float,
        charger_power_kw: float,
        surplus_forecast: List[SurplusForecast],
        price_forecast: List[PriceForecast],
    ) -> ChargingPlan:
        """
        Plan charging to minimize cost while meeting departure deadline.

        Strategy:
        1. Get all available charging windows before departure time
        2. Sort by price (cheapest first), with solar surplus as free (0 cost)
        3. Select cheapest windows until energy requirement is met
        4. If deadline is tight, prioritize meeting deadline over cost

        Example scenarios:
        - Plugged in at 11am with 1c/kWh price -> charge immediately
        - Arrive home 6pm at 58c/kWh, depart 6am with 15-20c overnight -> wait for cheap overnight
        """
        now = datetime.now()

        # Normalize target_time to match now's timezone awareness
        if target_time:
            if target_time.tzinfo is not None and now.tzinfo is None:
                try:
                    now = datetime.now(target_time.tzinfo)
                except Exception:
                    target_time = target_time.replace(tzinfo=None)
            elif target_time.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)

        _LOGGER.info(
            f"Planning cost-optimized charging: need {energy_needed_kwh:.1f}kWh, "
            f"charger={charger_power_kw}kW, target_time={target_time}"
        )

        # Build charging options from price forecast (within deadline)
        charging_options = []

        for i, price in enumerate(price_forecast):
            try:
                hour_dt = datetime.fromisoformat(price.hour)
                # Normalize hour_dt to match now's timezone awareness
                if hour_dt.tzinfo is not None and now.tzinfo is None:
                    hour_dt = hour_dt.replace(tzinfo=None)
                elif hour_dt.tzinfo is None and now.tzinfo is not None:
                    hour_dt = hour_dt.replace(tzinfo=now.tzinfo)
            except:
                continue

            # Skip if past departure time
            if target_time and hour_dt >= target_time:
                continue

            # Skip if in the past
            if hour_dt < now - timedelta(hours=1):
                continue

            # Check for solar surplus at this hour
            solar_available = 0
            if i < len(surplus_forecast):
                solar_available = surplus_forecast[i].surplus_kw

            # Solar surplus is free
            if solar_available >= 1.0:
                charging_options.append({
                    "hour": price.hour,
                    "hour_dt": hour_dt,
                    "source": "solar_surplus",
                    "power_kw": min(solar_available, charger_power_kw),
                    "cost_cents": 0,  # Solar is free
                    "actual_price": price.import_cents,  # Store actual price for reference
                    "confidence": surplus_forecast[i].confidence if i < len(surplus_forecast) else 0.5,
                })

            # Grid option
            grid_power = charger_power_kw - max(0, solar_available)
            if grid_power > 0.5:  # At least 0.5kW from grid
                charging_options.append({
                    "hour": price.hour,
                    "hour_dt": hour_dt,
                    "source": f"grid_{price.period}",
                    "power_kw": grid_power,
                    "cost_cents": price.import_cents,
                    "actual_price": price.import_cents,
                    "confidence": 0.95,
                })

        # Log available options
        if charging_options:
            prices = [opt["cost_cents"] for opt in charging_options]
            _LOGGER.info(
                f"Found {len(charging_options)} charging options, "
                f"prices range: {min(prices):.1f}c - {max(prices):.1f}c"
            )

        # Sort by cost (cheapest first)
        # Secondary sort by time to prefer earlier slots at same price
        charging_options.sort(key=lambda x: (x["cost_cents"], x["hour_dt"]))

        # Log top 5 cheapest options
        for i, opt in enumerate(charging_options[:5]):
            _LOGGER.debug(
                f"  Option {i+1}: {opt['hour_dt'].strftime('%H:%M')} - "
                f"{opt['cost_cents']:.1f}c/kWh ({opt['source']})"
            )

        # Allocate energy to cheapest windows
        windows = []
        energy_allocated = 0
        solar_energy = 0
        grid_energy = 0
        total_cost = 0
        used_hours = set()

        for option in charging_options:
            if energy_allocated >= energy_needed_kwh:
                break

            # Skip if already used this hour
            hour_key = option["hour_dt"].strftime("%Y-%m-%dT%H")
            if hour_key in used_hours:
                continue

            energy_this_hour = min(option["power_kw"], energy_needed_kwh - energy_allocated)
            hour_dt = option["hour_dt"]
            end_dt = hour_dt + timedelta(hours=1)

            windows.append(PlannedChargingWindow(
                start_time=option["hour"],
                end_time=end_dt.isoformat(),
                source=option["source"],
                estimated_power_kw=option["power_kw"],
                estimated_energy_kwh=energy_this_hour,
                price_cents_kwh=option["cost_cents"],
                reason="cost_optimized",
            ))

            energy_allocated += energy_this_hour
            if "solar" in option["source"]:
                solar_energy += energy_this_hour
            else:
                grid_energy += energy_this_hour
                total_cost += energy_this_hour * option["cost_cents"]

            used_hours.add(hour_key)

        # Sort windows by time for display
        windows.sort(key=lambda w: w.start_time)

        # Calculate if we can meet target
        can_meet = energy_allocated >= energy_needed_kwh * 0.9

        # Log the plan
        _LOGGER.info(
            f"Cost-optimized plan: {len(windows)} windows, "
            f"{solar_energy:.1f}kWh solar + {grid_energy:.1f}kWh grid, "
            f"est cost ${total_cost/100:.2f}, can_meet={can_meet}"
        )

        # Log each window
        for w in windows:
            _LOGGER.debug(
                f"  Window: {w.start_time[:16]} - {w.price_cents_kwh:.1f}c/kWh "
                f"({w.source}, {w.estimated_energy_kwh:.1f}kWh)"
            )

        plan = ChargingPlan(
            vehicle_id=vehicle_id,
            current_soc=current_soc,
            target_soc=target_soc,
            target_time=target_time.isoformat() if target_time else None,
            energy_needed_kwh=energy_needed_kwh,
            windows=windows,
            estimated_solar_kwh=solar_energy,
            estimated_grid_kwh=grid_energy,
            estimated_cost_cents=total_cost,
            confidence=0.8 if can_meet else 0.5,
            can_meet_target=can_meet,
        )

        return plan

    async def _plan_time_critical(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        energy_needed_kwh: float,
        charger_power_kw: float,
        surplus_forecast: List[SurplusForecast],
        price_forecast: List[PriceForecast],
    ) -> ChargingPlan:
        """Plan charging to meet deadline, minimizing cost as secondary goal."""
        if not target_time:
            # No deadline, use cost-optimized
            return await self._plan_cost_optimized(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
            )

        # Calculate minimum hours needed
        hours_needed = energy_needed_kwh / charger_power_kw
        # Ensure both datetimes are comparable (handle timezone-aware vs naive)
        now = datetime.now()
        if target_time.tzinfo is not None and now.tzinfo is None:
            try:
                now = datetime.now(target_time.tzinfo)
            except Exception:
                target_time = target_time.replace(tzinfo=None)
        elif target_time.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        hours_available = max(1, int((target_time - now).total_seconds() / 3600))

        if hours_needed > hours_available:
            # Can't meet target even charging continuously
            warning = f"Need {hours_needed:.1f}h but only {hours_available}h available"
        else:
            warning = None

        # Work backwards from deadline
        windows = []
        energy_allocated = 0
        solar_energy = 0
        grid_energy = 0
        total_cost = 0

        # Reverse the forecasts to work backwards
        combined = list(zip(surplus_forecast, price_forecast))
        combined.reverse()

        for surplus, price in combined:
            if energy_allocated >= energy_needed_kwh:
                break

            hour_dt = datetime.fromisoformat(surplus.hour)
            if target_time and hour_dt >= target_time:
                continue  # Skip hours after deadline

            # Use whatever is available
            if surplus.surplus_kw >= 1.0:
                # Prefer solar
                energy_this_hour = min(surplus.surplus_kw, charger_power_kw)
                source = "solar_surplus"
                cost = 0
                solar_energy += min(energy_this_hour, energy_needed_kwh - energy_allocated)
            else:
                # Use grid
                energy_this_hour = charger_power_kw
                source = f"grid_{price.period}"
                cost = price.import_cents
                grid_energy += min(energy_this_hour, energy_needed_kwh - energy_allocated)

            energy_this_hour = min(energy_this_hour, energy_needed_kwh - energy_allocated)

            end_dt = hour_dt + timedelta(hours=1)

            windows.append(PlannedChargingWindow(
                start_time=surplus.hour,
                end_time=end_dt.isoformat(),
                source=source,
                estimated_power_kw=charger_power_kw,
                estimated_energy_kwh=energy_this_hour,
                price_cents_kwh=cost,
                reason="target_deadline",
            ))

            energy_allocated += energy_this_hour
            total_cost += energy_this_hour * cost

        # Sort chronologically
        windows.sort(key=lambda w: w.start_time)

        plan = ChargingPlan(
            vehicle_id=vehicle_id,
            current_soc=current_soc,
            target_soc=target_soc,
            target_time=target_time.isoformat(),
            energy_needed_kwh=energy_needed_kwh,
            windows=windows,
            estimated_solar_kwh=solar_energy,
            estimated_grid_kwh=grid_energy,
            estimated_cost_cents=total_cost,
            confidence=0.8,
            can_meet_target=energy_allocated >= energy_needed_kwh * 0.9,
            warning=warning,
        )

        return plan

    async def should_charge_now(
        self,
        vehicle_id: str,
        plan: ChargingPlan,
        current_surplus_kw: float,
        current_price_cents: float,
        battery_soc: float,
        min_battery_soc: int = 80,
    ) -> Tuple[bool, str, str]:
        """
        Real-time decision: should we charge right now?

        Strategy:
        1. Always respect home battery priority (min SoC)
        2. If in a planned window from cost-optimized plan, charge
        3. Opportunistic: charge on solar surplus (free)
        4. Opportunistic: charge if current price is very cheap (< plan avg or < 10c)
        5. Otherwise wait for planned windows or better prices

        Args:
            vehicle_id: Vehicle identifier
            plan: Current charging plan
            current_surplus_kw: Current solar surplus
            current_price_cents: Current import price
            battery_soc: Current home battery SoC
            min_battery_soc: Minimum home battery SoC before EV charging

        Returns:
            Tuple of (should_charge, reason, source)
        """
        now = datetime.now()

        # Check if battery priority is met
        if battery_soc < min_battery_soc:
            return False, f"Battery at {battery_soc:.0f}% (min: {min_battery_soc}%)", "waiting"

        # Check if we're in a planned window
        for window in plan.windows:
            try:
                window_start = datetime.fromisoformat(window.start_time)
                window_end = datetime.fromisoformat(window.end_time)

                if window_start <= now < window_end:
                    _LOGGER.debug(
                        f"In planned window: {window_start.strftime('%H:%M')}-{window_end.strftime('%H:%M')} "
                        f"({window.source}, {window.price_cents_kwh:.1f}c/kWh)"
                    )
                    return True, f"In planned {window.source} window ({window.price_cents_kwh:.0f}c)", window.source
            except Exception as e:
                _LOGGER.debug(f"Error parsing window time: {e}")
                continue

        # Check for opportunistic solar (always take free power)
        if current_surplus_kw >= 1.5:
            return True, f"Solar surplus ({current_surplus_kw:.1f}kW)", "solar_surplus"

        # Calculate average planned price for comparison
        if plan.windows:
            planned_prices = [w.price_cents_kwh for w in plan.windows if w.price_cents_kwh > 0]
            avg_planned_price = sum(planned_prices) / len(planned_prices) if planned_prices else 30
            min_planned_price = min(planned_prices) if planned_prices else 30
        else:
            avg_planned_price = 30
            min_planned_price = 30

        # Opportunistic: if current price is better than our best planned window, charge now
        # This handles the case where prices dropped since we made the plan
        if current_price_cents <= min_planned_price and current_price_cents < 20:
            _LOGGER.info(
                f"Opportunistic charging: current {current_price_cents:.1f}c <= "
                f"best planned {min_planned_price:.1f}c"
            )
            return True, f"Better than planned ({current_price_cents:.0f}c ≤ {min_planned_price:.0f}c)", "grid_opportunistic"

        # Opportunistic: very cheap power (< 10c) - always charge
        if current_price_cents < 10:
            return True, f"Very cheap power ({current_price_cents:.0f}c/kWh)", "grid_offpeak"

        # Opportunistic: negative pricing (getting paid to use power)
        if current_price_cents < 0:
            return True, f"Negative pricing ({current_price_cents:.0f}c/kWh) - getting paid!", "grid_negative"

        # Check how far away the next planned window is
        next_window_start = None
        for window in sorted(plan.windows, key=lambda w: w.start_time):
            try:
                window_start = datetime.fromisoformat(window.start_time)
                if window_start > now:
                    next_window_start = window_start
                    break
            except:
                continue

        if next_window_start:
            hours_until = (next_window_start - now).total_seconds() / 3600
            return False, f"Waiting for {next_window_start.strftime('%H:%M')} ({hours_until:.1f}h, {min_planned_price:.0f}c)", "waiting"

        return False, f"Waiting for better rates (current: {current_price_cents:.0f}c)", "waiting"


# Global planner instance (initialized by __init__.py)
_charging_planner: Optional[ChargingPlanner] = None


def get_charging_planner() -> Optional[ChargingPlanner]:
    """Get the global charging planner instance."""
    return _charging_planner


def set_charging_planner(planner: ChargingPlanner) -> None:
    """Set the global charging planner instance."""
    global _charging_planner
    _charging_planner = planner


# =============================================================================
# Auto-Schedule Executor
# =============================================================================

@dataclass
class AutoScheduleSettings:
    """Settings for automatic schedule execution per vehicle."""
    enabled: bool = False
    vehicle_id: str = "_default"
    display_name: str = "EV"

    # Target settings
    target_soc: int = 80
    departure_time: Optional[str] = None  # HH:MM format
    departure_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri

    # Priority mode
    priority: ChargingPriority = ChargingPriority.COST_OPTIMIZED

    # Constraints
    min_battery_soc: int = 80  # Home battery must be above this before EV charging
    max_grid_price_cents: float = 25.0  # Don't charge from grid above this price
    min_surplus_kw: float = 1.5  # Minimum solar surplus to charge

    # Charger settings
    charger_type: str = "tesla"  # tesla, ocpp, generic
    min_charge_amps: int = 5
    max_charge_amps: int = 32
    voltage: int = 240

    # Optional entity overrides for generic chargers
    charger_switch_entity: Optional[str] = None
    charger_amps_entity: Optional[str] = None
    ocpp_charger_id: Optional[int] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "enabled": self.enabled,
            "vehicle_id": self.vehicle_id,
            "display_name": self.display_name,
            "target_soc": self.target_soc,
            "departure_time": self.departure_time,
            "departure_days": self.departure_days,
            "priority": self.priority.value,
            "min_battery_soc": self.min_battery_soc,
            "max_grid_price_cents": self.max_grid_price_cents,
            "min_surplus_kw": self.min_surplus_kw,
            "charger_type": self.charger_type,
            "min_charge_amps": self.min_charge_amps,
            "max_charge_amps": self.max_charge_amps,
            "voltage": self.voltage,
            "charger_switch_entity": self.charger_switch_entity,
            "charger_amps_entity": self.charger_amps_entity,
            "ocpp_charger_id": self.ocpp_charger_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AutoScheduleSettings":
        """Create from dictionary."""
        priority_str = data.get("priority", "cost_optimized")
        try:
            priority = ChargingPriority(priority_str)
        except ValueError:
            priority = ChargingPriority.COST_OPTIMIZED

        return cls(
            enabled=data.get("enabled", False),
            vehicle_id=data.get("vehicle_id", "_default"),
            display_name=data.get("display_name", "EV"),
            target_soc=data.get("target_soc", 80),
            departure_time=data.get("departure_time"),
            departure_days=data.get("departure_days", [0, 1, 2, 3, 4]),
            priority=priority,
            min_battery_soc=data.get("min_battery_soc", 80),
            max_grid_price_cents=data.get("max_grid_price_cents", 25.0),
            min_surplus_kw=data.get("min_surplus_kw", 1.5),
            charger_type=data.get("charger_type", "tesla"),
            min_charge_amps=data.get("min_charge_amps", 5),
            max_charge_amps=data.get("max_charge_amps", 32),
            voltage=data.get("voltage", 240),
            charger_switch_entity=data.get("charger_switch_entity"),
            charger_amps_entity=data.get("charger_amps_entity"),
            ocpp_charger_id=data.get("ocpp_charger_id"),
        )


@dataclass
class AutoScheduleState:
    """Current state of auto-schedule execution for a vehicle."""
    vehicle_id: str
    is_charging: bool = False
    current_window: Optional[PlannedChargingWindow] = None
    current_plan: Optional[ChargingPlan] = None
    last_plan_update: Optional[datetime] = None
    last_decision: str = "idle"
    last_decision_reason: str = ""
    started_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for API."""
        return {
            "vehicle_id": self.vehicle_id,
            "is_charging": self.is_charging,
            "current_window": {
                "start_time": self.current_window.start_time,
                "end_time": self.current_window.end_time,
                "source": self.current_window.source,
                "price_cents_kwh": self.current_window.price_cents_kwh,
            } if self.current_window else None,
            "last_decision": self.last_decision,
            "last_decision_reason": self.last_decision_reason,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "plan_summary": {
                "windows": len(self.current_plan.windows) if self.current_plan else 0,
                "estimated_solar_kwh": self.current_plan.estimated_solar_kwh if self.current_plan else 0,
                "estimated_grid_kwh": self.current_plan.estimated_grid_kwh if self.current_plan else 0,
                "estimated_cost_cents": self.current_plan.estimated_cost_cents if self.current_plan else 0,
            } if self.current_plan else None,
        }


class AutoScheduleExecutor:
    """
    Automatically executes charging plans based on optimal windows.

    Integrates with:
    - ChargingPlanner for optimal window generation
    - PriceForecaster for Amber/Globird/FlowPower pricing
    - SolarForecaster for Solcast surplus predictions
    - Dynamic EV charging actions for actual control
    """

    def __init__(self, hass, config_entry, planner: ChargingPlanner):
        self.hass = hass
        self.config_entry = config_entry
        self.planner = planner

        # Settings per vehicle (loaded from storage)
        self._settings: Dict[str, AutoScheduleSettings] = {}

        # Runtime state per vehicle
        self._state: Dict[str, AutoScheduleState] = {}

        # Plan regeneration interval (regenerate every 15 minutes)
        self._plan_update_interval = timedelta(minutes=15)

    async def load_settings(self, store) -> None:
        """Load settings from storage."""
        try:
            stored_data = await store.async_load() if hasattr(store, 'async_load') else {}
            if not stored_data:
                stored_data = {}

            auto_schedule_data = stored_data.get("auto_schedule_settings", {})

            for vehicle_id, settings_dict in auto_schedule_data.items():
                self._settings[vehicle_id] = AutoScheduleSettings.from_dict(settings_dict)
                self._state[vehicle_id] = AutoScheduleState(vehicle_id=vehicle_id)

            _LOGGER.debug(f"Loaded auto-schedule settings for {len(self._settings)} vehicles")
        except Exception as e:
            _LOGGER.error(f"Failed to load auto-schedule settings: {e}")

    async def save_settings(self, store) -> None:
        """Save settings to storage."""
        try:
            stored_data = await store.async_load() if hasattr(store, 'async_load') else {}
            if not stored_data:
                stored_data = {}

            auto_schedule_data = {}
            for vehicle_id, settings in self._settings.items():
                auto_schedule_data[vehicle_id] = settings.to_dict()

            stored_data["auto_schedule_settings"] = auto_schedule_data

            if hasattr(store, 'async_save'):
                store._data = stored_data
                await store.async_save(stored_data)

            _LOGGER.debug(f"Saved auto-schedule settings for {len(self._settings)} vehicles")
        except Exception as e:
            _LOGGER.error(f"Failed to save auto-schedule settings: {e}")

    def get_settings(self, vehicle_id: str) -> AutoScheduleSettings:
        """Get settings for a vehicle, creating defaults if needed."""
        if vehicle_id not in self._settings:
            self._settings[vehicle_id] = AutoScheduleSettings(vehicle_id=vehicle_id)
            self._state[vehicle_id] = AutoScheduleState(vehicle_id=vehicle_id)
        return self._settings[vehicle_id]

    def update_settings(self, vehicle_id: str, updates: dict) -> AutoScheduleSettings:
        """Update settings for a vehicle."""
        settings = self.get_settings(vehicle_id)

        for key, value in updates.items():
            if key == "priority" and isinstance(value, str):
                try:
                    value = ChargingPriority(value)
                except ValueError:
                    continue
            if hasattr(settings, key):
                setattr(settings, key, value)

        return settings

    def get_state(self, vehicle_id: str) -> AutoScheduleState:
        """Get current state for a vehicle."""
        if vehicle_id not in self._state:
            self._state[vehicle_id] = AutoScheduleState(vehicle_id=vehicle_id)
        return self._state[vehicle_id]

    def get_all_states(self) -> Dict[str, dict]:
        """Get all vehicle states."""
        return {vid: state.to_dict() for vid, state in self._state.items()}

    async def evaluate(self, live_status: dict, current_price_cents: Optional[float] = None) -> None:
        """
        Evaluate all vehicles and start/stop charging as needed.

        This should be called periodically (e.g., every 30-60 seconds).

        Args:
            live_status: Current Powerwall/system status with battery_soc, solar_power, etc.
            current_price_cents: Current import price (from Amber/tariff)
        """
        for vehicle_id, settings in self._settings.items():
            if not settings.enabled:
                continue

            try:
                await self._evaluate_vehicle(vehicle_id, settings, live_status, current_price_cents)
            except Exception as e:
                _LOGGER.error(f"Auto-schedule evaluation failed for {vehicle_id}: {e}")

    async def _evaluate_vehicle(
        self,
        vehicle_id: str,
        settings: AutoScheduleSettings,
        live_status: dict,
        current_price_cents: Optional[float],
    ) -> None:
        """Evaluate and control charging for a single vehicle."""
        state = self.get_state(vehicle_id)
        now = datetime.now()

        # Check if we need to regenerate the plan
        if (
            state.current_plan is None or
            state.last_plan_update is None or
            now - state.last_plan_update > self._plan_update_interval
        ):
            await self._regenerate_plan(vehicle_id, settings, state)

        if state.current_plan is None:
            state.last_decision = "no_plan"
            state.last_decision_reason = "No charging plan available"
            return

        # Get current conditions
        battery_soc = live_status.get("battery_soc", 0)
        solar_power_kw = live_status.get("solar_power", 0) / 1000
        grid_power_kw = live_status.get("grid_power", 0) / 1000
        load_power_kw = live_status.get("load_power", 0) / 1000

        # Calculate current surplus
        current_surplus_kw = max(0, solar_power_kw - load_power_kw)

        # Use price from parameter or estimate
        if current_price_cents is None:
            current_price_cents = await self._get_current_price()

        # Check battery priority
        if battery_soc < settings.min_battery_soc:
            if state.is_charging:
                await self._stop_charging(vehicle_id, settings, state)
                state.last_decision = "stopped"
                state.last_decision_reason = f"Battery {battery_soc:.0f}% < min {settings.min_battery_soc}%"
            else:
                state.last_decision = "waiting"
                state.last_decision_reason = f"Battery {battery_soc:.0f}% < min {settings.min_battery_soc}%"
            return

        # Use planner's should_charge_now logic
        should_charge, reason, source = await self.planner.should_charge_now(
            vehicle_id=vehicle_id,
            plan=state.current_plan,
            current_surplus_kw=current_surplus_kw,
            current_price_cents=current_price_cents,
            battery_soc=battery_soc,
            min_battery_soc=settings.min_battery_soc,
        )

        # Apply additional constraints based on priority mode
        if should_charge and source.startswith("grid"):
            # Check price constraint
            if current_price_cents > settings.max_grid_price_cents:
                should_charge = False
                reason = f"Grid price {current_price_cents:.0f}c > max {settings.max_grid_price_cents:.0f}c"

            # Solar-only mode doesn't allow grid
            if settings.priority == ChargingPriority.SOLAR_ONLY:
                should_charge = False
                reason = "Solar-only mode - no grid charging"

        # Check surplus constraint for solar charging
        if should_charge and source == "solar_surplus":
            if current_surplus_kw < settings.min_surplus_kw:
                should_charge = False
                reason = f"Surplus {current_surplus_kw:.1f}kW < min {settings.min_surplus_kw:.1f}kW"

        # Find current window (if in one)
        current_window = None
        for window in state.current_plan.windows:
            window_start = datetime.fromisoformat(window.start_time)
            window_end = datetime.fromisoformat(window.end_time)
            if window_start <= now < window_end:
                current_window = window
                break

        state.current_window = current_window

        # Take action
        if should_charge and not state.is_charging:
            await self._start_charging(vehicle_id, settings, state, source)
            state.last_decision = "started"
            state.last_decision_reason = reason
        elif not should_charge and state.is_charging:
            await self._stop_charging(vehicle_id, settings, state)
            state.last_decision = "stopped"
            state.last_decision_reason = reason
        else:
            state.last_decision = "charging" if state.is_charging else "waiting"
            state.last_decision_reason = reason

    async def _regenerate_plan(
        self,
        vehicle_id: str,
        settings: AutoScheduleSettings,
        state: AutoScheduleState,
    ) -> None:
        """Regenerate the charging plan based on current forecasts."""
        now = datetime.now()

        # Determine target time
        target_time = None
        if settings.departure_time:
            # Parse departure time
            try:
                dep_hour, dep_min = map(int, settings.departure_time.split(":"))
                target_time = now.replace(hour=dep_hour, minute=dep_min, second=0, microsecond=0)

                # If departure is in the past today, use tomorrow
                if target_time <= now:
                    target_time += timedelta(days=1)

                # Check if target day is in departure_days
                while target_time.weekday() not in settings.departure_days:
                    target_time += timedelta(days=1)
            except ValueError:
                _LOGGER.warning(f"Invalid departure time format: {settings.departure_time}")

        # Get current SoC (estimate - could be improved with actual EV API)
        current_soc = 50  # Default estimate

        try:
            plan = await self.planner.plan_charging(
                vehicle_id=vehicle_id,
                current_soc=current_soc,
                target_soc=settings.target_soc,
                target_time=target_time,
                priority=settings.priority,
                charger_power_kw=(settings.max_charge_amps * settings.voltage) / 1000,
            )

            state.current_plan = plan
            state.last_plan_update = now

            _LOGGER.info(
                f"Auto-schedule: Regenerated plan for {vehicle_id} - "
                f"{len(plan.windows)} windows, {plan.estimated_solar_kwh:.1f}kWh solar, "
                f"{plan.estimated_grid_kwh:.1f}kWh grid, ${plan.estimated_cost_cents/100:.2f} est cost"
            )
        except Exception as e:
            _LOGGER.error(f"Failed to regenerate plan for {vehicle_id}: {e}")

    async def _get_current_price(self) -> float:
        """Get current import price from available sources (provider-aware)."""
        from ..const import DOMAIN, CONF_ELECTRICITY_PROVIDER

        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})

            # Get electricity provider
            electricity_provider = self.config_entry.options.get(
                CONF_ELECTRICITY_PROVIDER,
                self.config_entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
            )

            if electricity_provider in ("amber", "flow_power"):
                # Amber/Flow Power: Read from coordinator data
                amber_coordinator = entry_data.get("amber_coordinator")
                if amber_coordinator and amber_coordinator.data:
                    current_prices = amber_coordinator.data.get("current", [])
                    for price in current_prices:
                        if price.get("channelType") == "general":
                            # perKwh is in cents for Amber
                            return price.get("perKwh", 30.0)

            elif electricity_provider in ("globird", "aemo_vpp"):
                # Globird: Read from tariff schedule (populated by TariffPriceView)
                tariff_schedule = entry_data.get("tariff_schedule", {})
                if tariff_schedule:
                    buy_price = tariff_schedule.get("buy_price")
                    if buy_price is not None:
                        return buy_price  # Already in cents

            # Fallback: Try tariff schedule for any provider
            tariff_schedule = entry_data.get("tariff_schedule", {})
            if tariff_schedule:
                now = datetime.now()
                period_key = f"PERIOD_{now.hour:02d}_{30 if now.minute >= 30 else 0:02d}"
                buy_prices = tariff_schedule.get("buy_prices", {})
                if period_key in buy_prices:
                    return buy_prices[period_key] * 100

            # Default fallback based on time of day
            hour = datetime.now().hour
            if 7 <= hour < 9 or 17 <= hour < 21:
                return 45.0  # Peak
            elif 9 <= hour < 17:
                return 25.0  # Shoulder
            else:
                return 15.0  # Off-peak

        except Exception as e:
            _LOGGER.debug(f"Failed to get current price: {e}")
            return 25.0  # Default shoulder rate

    async def _start_charging(
        self,
        vehicle_id: str,
        settings: AutoScheduleSettings,
        state: AutoScheduleState,
        source: str,
    ) -> None:
        """Start dynamic charging for the vehicle."""
        from .actions import _action_start_ev_charging_dynamic

        # Determine mode based on source
        if source == "solar_surplus":
            dynamic_mode = "solar_surplus"
        else:
            dynamic_mode = "battery_target"

        params = {
            "vehicle_vin": vehicle_id if vehicle_id != "_default" else None,
            "dynamic_mode": dynamic_mode,
            "min_charge_amps": settings.min_charge_amps,
            "max_charge_amps": settings.max_charge_amps,
            "voltage": settings.voltage,
            "charger_type": settings.charger_type,
            "min_battery_soc": settings.min_battery_soc,
            "pause_below_soc": settings.min_battery_soc - 10,
            "charger_switch_entity": settings.charger_switch_entity,
            "charger_amps_entity": settings.charger_amps_entity,
            "ocpp_charger_id": settings.ocpp_charger_id,
        }

        try:
            success = await _action_start_ev_charging_dynamic(
                self.hass, self.config_entry, params, context=None
            )

            if success:
                state.is_charging = True
                state.started_at = datetime.now()
                _LOGGER.info(f"Auto-schedule: Started {dynamic_mode} charging for {vehicle_id}")
            else:
                _LOGGER.warning(f"Auto-schedule: Failed to start charging for {vehicle_id}")
        except Exception as e:
            _LOGGER.error(f"Auto-schedule: Error starting charging for {vehicle_id}: {e}")

    async def _stop_charging(
        self,
        vehicle_id: str,
        settings: AutoScheduleSettings,
        state: AutoScheduleState,
    ) -> None:
        """Stop charging for the vehicle."""
        from .actions import _action_stop_ev_charging_dynamic

        params = {"vehicle_id": vehicle_id if vehicle_id != "_default" else None}

        try:
            await _action_stop_ev_charging_dynamic(self.hass, self.config_entry, params)
            state.is_charging = False
            state.started_at = None
            state.current_window = None
            _LOGGER.info(f"Auto-schedule: Stopped charging for {vehicle_id}")
        except Exception as e:
            _LOGGER.error(f"Auto-schedule: Error stopping charging for {vehicle_id}: {e}")


# Global auto-schedule executor instance
_auto_schedule_executor: Optional[AutoScheduleExecutor] = None


def get_auto_schedule_executor() -> Optional[AutoScheduleExecutor]:
    """Get the global auto-schedule executor instance."""
    return _auto_schedule_executor


def set_auto_schedule_executor(executor: AutoScheduleExecutor) -> None:
    """Set the global auto-schedule executor instance."""
    global _auto_schedule_executor
    _auto_schedule_executor = executor
