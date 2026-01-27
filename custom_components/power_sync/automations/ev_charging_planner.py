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
        Get hourly price forecast.

        Tries Amber API first, falls back to TOU estimation.

        Args:
            hours: Number of hours to forecast

        Returns:
            List of PriceForecast objects
        """
        # Try Amber forecast
        amber_forecast = await self._get_amber_forecast(hours)
        if amber_forecast:
            return amber_forecast

        # Fall back to TOU estimation
        return await self._estimate_tou_prices(hours)

    async def _get_amber_forecast(self, hours: int) -> Optional[List[PriceForecast]]:
        """Get forecast from Amber API if configured."""
        try:
            from ..const import DOMAIN, CONF_AMBER_API_TOKEN

            # Check if Amber is configured
            amber_token = self.config_entry.data.get(CONF_AMBER_API_TOKEN)
            if not amber_token:
                return None

            # Try to get forecast from stored data
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            price_data = entry_data.get("price_forecast", {})

            if not price_data:
                return None

            forecasts = []
            now = datetime.now()

            for h in range(hours):
                hour_dt = now + timedelta(hours=h)
                hour_key = hour_dt.strftime("%Y-%m-%dT%H:00")

                # Get price for this hour
                hour_price = price_data.get(hour_key, {})
                import_cents = hour_price.get("per_kwh", 30)  # Default 30c
                export_cents = hour_price.get("feed_in_tariff", 8)  # Default 8c

                # Determine period
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

            return forecasts

        except Exception as e:
            _LOGGER.debug(f"Could not get Amber forecast: {e}")
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
            hours_available = max(1, int((target_time - datetime.now()).total_seconds() / 3600))
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
        """Plan charging to minimize cost."""
        # Combine forecasts and sort by effective cost
        charging_options = []

        for i, surplus in enumerate(surplus_forecast):
            if i < len(price_forecast):
                price = price_forecast[i]

                # Solar surplus is free
                if surplus.surplus_kw >= 1.0:
                    charging_options.append({
                        "hour": surplus.hour,
                        "source": "solar_surplus",
                        "power_kw": min(surplus.surplus_kw, charger_power_kw),
                        "cost_cents": 0,
                        "confidence": surplus.confidence,
                    })

                # Grid option (if no/insufficient solar)
                remaining_power = charger_power_kw - max(0, surplus.surplus_kw)
                if remaining_power > 0:
                    charging_options.append({
                        "hour": price.hour,
                        "source": f"grid_{price.period}",
                        "power_kw": remaining_power,
                        "cost_cents": price.import_cents,
                        "confidence": 0.95,
                    })

        # Sort by cost (cheapest first)
        charging_options.sort(key=lambda x: x["cost_cents"])

        # Allocate energy
        windows = []
        energy_allocated = 0
        solar_energy = 0
        grid_energy = 0
        total_cost = 0

        used_hours = set()

        for option in charging_options:
            if energy_allocated >= energy_needed_kwh:
                break

            if option["hour"] in used_hours:
                continue

            energy_this_hour = min(option["power_kw"], energy_needed_kwh - energy_allocated)

            hour_dt = datetime.fromisoformat(option["hour"])
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

            used_hours.add(option["hour"])

        # Sort by time for display
        windows.sort(key=lambda w: w.start_time)

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
            confidence=0.7,
            can_meet_target=energy_allocated >= energy_needed_kwh * 0.9,
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
        hours_available = max(1, int((target_time - datetime.now()).total_seconds() / 3600))

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
            window_start = datetime.fromisoformat(window.start_time)
            window_end = datetime.fromisoformat(window.end_time)

            if window_start <= now < window_end:
                return True, f"In planned {window.source} window", window.source

        # Check for opportunistic solar
        if current_surplus_kw >= 1.5:
            return True, f"Solar surplus available ({current_surplus_kw:.1f}kW)", "solar_surplus"

        # Check for cheap grid
        if current_price_cents < 15:
            return True, f"Offpeak rate ({current_price_cents:.0f}c/kWh)", "grid_offpeak"

        return False, "Waiting for better conditions", "waiting"


# Global planner instance (initialized by __init__.py)
_charging_planner: Optional[ChargingPlanner] = None


def get_charging_planner() -> Optional[ChargingPlanner]:
    """Get the global charging planner instance."""
    return _charging_planner


def set_charging_planner(planner: ChargingPlanner) -> None:
    """Set the global charging planner instance."""
    global _charging_planner
    _charging_planner = planner
