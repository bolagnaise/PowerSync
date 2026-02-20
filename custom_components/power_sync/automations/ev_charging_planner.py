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

import aiohttp

from ..const import TESLA_INTEGRATIONS

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


# ============================================================================
# Module-level helper functions for EV state detection
# ============================================================================

async def get_ev_location(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry",
    vehicle_vin: Optional[str] = None
) -> str:
    """
    Get EV location from Home Assistant entities.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        vehicle_vin: Optional VIN to check specific vehicle. If None, returns
                     location of first vehicle found (backward compatible).

    Returns:
        Location string: "home", "work", "not_home", or "unknown"
    """
    from ..const import (
        DOMAIN,
        CONF_TESLA_BLE_ENTITY_PREFIX,
        DEFAULT_TESLA_BLE_ENTITY_PREFIX,
        CONF_ZAPTEC_STANDALONE_ENABLED,
        CONF_ZAPTEC_USERNAME,
        CONF_OCPP_ENABLED,
    )
    from homeassistant.helpers import entity_registry as er, device_registry as dr

    location = "unknown"

    # Zaptec standalone — charger is at home by definition
    if config_entry:
        opts = {**config_entry.data, **config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            _LOGGER.debug("Zaptec standalone configured, assuming location=home")
            return "home"

    # OCPP — charger is at home by definition
    if config_entry:
        opts = {**config_entry.data, **config_entry.options}
        if opts.get(CONF_OCPP_ENABLED):
            _LOGGER.debug("OCPP charger configured, assuming location=home")
            return "home"

    # Method 0: Teslemetry Bluetooth - has real device_tracker with location
    import re
    for state in hass.states.async_all():
        match = re.match(r"sensor\.(\w+)_charging_state$", state.entity_id)
        if match:
            candidate = match.group(1)
            if len(candidate) == 17 and candidate.isalnum():
                if hass.states.get(f"switch.{candidate}_charge") is not None:
                    # Found Teslemetry BT prefix — check location
                    if vehicle_vin is not None and candidate.upper() != vehicle_vin.upper():
                        continue
                    loc_entity = f"device_tracker.{candidate}_location"
                    loc_state = hass.states.get(loc_entity)
                    if loc_state and loc_state.state not in ("unavailable", "unknown", "None", None):
                        location = loc_state.state.lower()
                        _LOGGER.debug(f"Teslemetry BT location from {loc_entity}: {location}")
                        return location

    # Method 1: Check Tesla Fleet/Teslemetry device_tracker entities
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    tesla_integrations = TESLA_INTEGRATIONS

    for device in device_registry.devices.values():
        if location != "unknown":
            break

        is_tesla_vehicle = False
        device_vin: Optional[str] = None
        for identifier in device.identifiers:
            if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                id_str = str(identifier[1])
                if len(id_str) == 17 and not id_str.isdigit():
                    is_tesla_vehicle = True
                    device_vin = id_str
                    break

        if not is_tesla_vehicle:
            continue

        # If specific VIN requested, skip other vehicles
        if vehicle_vin is not None and device_vin != vehicle_vin:
            continue

        for entity in entity_registry.entities.values():
            if entity.device_id != device.id:
                continue

            entity_id = entity.entity_id
            entity_id_lower = entity_id.lower()

            if entity_id.startswith("device_tracker.") and "_location" in entity_id_lower:
                state = hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", "None", None):
                    location = state.state.lower()
                    _LOGGER.debug(f"Found EV location from {entity_id} (VIN: {device_vin}): {location}")
                    break

            elif entity_id.startswith("binary_sensor.") and "located_at_home" in entity_id_lower:
                state = hass.states.get(entity_id)
                if state and state.state == "on":
                    location = "home"
                    _LOGGER.debug(f"Found EV at home from {entity_id} (VIN: {device_vin})")
                    break

    # Method 2 (fallback): Tesla BLE - if available, vehicle is nearby (assume "home")
    # Only used if no authoritative location found above (e.g. BLE-only users without Teslemetry)
    if location == "unknown" and vehicle_vin is None:
        config = dict(config_entry.options) if config_entry else {}
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
        ble_charger_entity = f"switch.{ble_prefix}_charger"
        ble_state = hass.states.get(ble_charger_entity)

        if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
            location = "home"
            _LOGGER.debug(f"Tesla BLE detected (fallback), assuming location=home")

    return location


async def discover_all_tesla_vehicles(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry"
) -> List[Dict[str, Any]]:
    """
    Discover all Tesla vehicles registered in Home Assistant.

    Searches the device registry for devices from known Tesla integrations
    and returns a list of all discovered vehicles with their VINs.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry

    Returns:
        List of dicts: [{"vin": str, "name": str, "device_id": str}, ...]
    """
    from homeassistant.helpers import device_registry as dr

    device_registry = dr.async_get(hass)
    vehicles: List[Dict[str, Any]] = []

    for device in device_registry.devices.values():
        for identifier in device.identifiers:
            if len(identifier) >= 2 and identifier[0] in TESLA_INTEGRATIONS:
                id_str = str(identifier[1])
                # VIN is 17 characters and not all digits (distinguish from other IDs)
                if len(id_str) == 17 and not id_str.isdigit():
                    vehicles.append({
                        "vin": id_str,
                        "name": device.name or device.name_by_user or id_str,
                        "device_id": device.id,
                    })
                    _LOGGER.debug(f"Discovered Tesla vehicle: {device.name} (VIN: {id_str})")
                    break

    _LOGGER.debug(f"Discovered {len(vehicles)} Tesla vehicle(s)")
    return vehicles


async def is_ev_plugged_in(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry",
    vehicle_vin: Optional[str] = None
) -> bool:
    """
    Check if EV is plugged in from Home Assistant entities.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        vehicle_vin: Optional VIN to check specific vehicle. If None, returns
                     True if any vehicle is plugged in (backward compatible).

    Returns:
        True if plugged in, False otherwise
    """
    from ..const import (
        DOMAIN,
        CONF_TESLA_BLE_ENTITY_PREFIX,
        DEFAULT_TESLA_BLE_ENTITY_PREFIX,
        CONF_ZAPTEC_STANDALONE_ENABLED,
        CONF_ZAPTEC_USERNAME,
        CONF_OCPP_ENABLED,
    )
    from homeassistant.helpers import entity_registry as er, device_registry as dr

    # Zaptec standalone — check cached state first
    if config_entry:
        opts = {**config_entry.data, **config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
            zaptec_cached = entry_data.get("zaptec_cached_state")
            if zaptec_cached:
                mode = zaptec_cached.get("charger_operation_mode", "")
                power_w = zaptec_cached.get("total_charge_power_w", 0)
                cable_locked = zaptec_cached.get("cable_locked", False)
                plugged = mode in ("charging", "connected_waiting") or power_w > 50 or cable_locked
                _LOGGER.debug("Zaptec plugged_in check: mode=%s, power=%sW, cable_locked=%s → %s",
                              mode, power_w, cable_locked, plugged)
                return plugged

    # OCPP — check charge point status
    if config_entry:
        opts = {**config_entry.data, **config_entry.options}
        if opts.get(CONF_OCPP_ENABLED):
            entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
            ocpp_server = entry_data.get("ocpp_server")
            if ocpp_server and hasattr(ocpp_server, 'charge_points'):
                for cp_id, cp in ocpp_server.charge_points.items():
                    # A charge point with an active transaction means vehicle is plugged in
                    if hasattr(cp, 'active_transaction') and cp.active_transaction:
                        _LOGGER.debug(f"OCPP charge point {cp_id} has active transaction → plugged in")
                        return True
                    # Also check connector status if available
                    status = getattr(cp, 'status', '').lower()
                    if status in ('preparing', 'charging', 'suspendedev', 'suspendedevse', 'finishing'):
                        _LOGGER.debug(f"OCPP charge point {cp_id} status={status} → plugged in")
                        return True
                _LOGGER.debug("OCPP server found but no charge point has vehicle connected")
                return False

    # Method 0: Teslemetry Bluetooth — check sensor.*_charging_state
    import re as _re
    for state in hass.states.async_all():
        match = _re.match(r"sensor\.(\w+)_charging_state$", state.entity_id)
        if match:
            candidate = match.group(1)
            if len(candidate) == 17 and candidate.isalnum():
                if hass.states.get(f"switch.{candidate}_charge") is not None:
                    if vehicle_vin is not None and candidate.upper() != vehicle_vin.upper():
                        continue
                    cs = hass.states.get(f"sensor.{candidate}_charging_state")
                    if cs and cs.state not in ("unavailable", "unknown", "Disconnected", "None", None):
                        _LOGGER.debug(f"Teslemetry BT: vehicle plugged in (state={cs.state})")
                        return True

    # Method 1: Check Tesla Fleet/Teslemetry entities
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    tesla_integrations = TESLA_INTEGRATIONS

    for device in device_registry.devices.values():
        is_tesla_vehicle = False
        device_vin: Optional[str] = None
        for identifier in device.identifiers:
            if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                id_str = str(identifier[1])
                if len(id_str) == 17 and not id_str.isdigit():
                    is_tesla_vehicle = True
                    device_vin = id_str
                    break

        if not is_tesla_vehicle:
            continue

        # If specific VIN requested, skip other vehicles
        if vehicle_vin is not None and device_vin != vehicle_vin:
            continue

        for entity in entity_registry.entities.values():
            if entity.device_id != device.id:
                continue

            entity_id = entity.entity_id
            entity_id_lower = entity_id.lower()

            if entity_id.startswith("binary_sensor.") and "charge_cable" in entity_id_lower:
                state = hass.states.get(entity_id)
                if state:
                    if state.state in ("unavailable", "unknown"):
                        # Car likely asleep — check location to determine if still plugged in
                        location = await get_ev_location(hass, config_entry, device_vin)
                        if location == "home":
                            _LOGGER.debug(f"Charge cable {entity_id} is {state.state} but car is home, treating as plugged in")
                            return True
                        else:
                            _LOGGER.debug(f"Charge cable {entity_id} is {state.state} and car not home, treating as unplugged")
                            return False
                    is_plugged = state.state == "on"
                    _LOGGER.debug(f"Found plugged in state from {entity_id} (VIN: {device_vin}): {is_plugged}")
                    return is_plugged

            elif entity_id.startswith("sensor.") and "_charging" in entity_id_lower and "charging_" not in entity_id_lower:
                state = hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", "None", None):
                    if state.state.lower() in ("charging", "complete", "stopped"):
                        _LOGGER.debug(f"EV plugged in (charging state: {state.state}, VIN: {device_vin})")
                        return True

    # Method 2 (fallback): Tesla BLE — only if no authoritative sensor found above
    if vehicle_vin is None:
        config = dict(config_entry.options) if config_entry else {}
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
        ble_charger_entity = f"switch.{ble_prefix}_charger"
        ble_state = hass.states.get(ble_charger_entity)

        if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
            _LOGGER.debug(f"Tesla BLE detected (fallback), assuming plugged in")
            return True

    return False


async def get_ev_battery_level(
    hass: "HomeAssistant",
    config_entry: "ConfigEntry",
    vehicle_vin: Optional[str] = None
) -> Optional[float]:
    """
    Get EV battery level (SOC) from Home Assistant entities.

    Args:
        hass: Home Assistant instance
        config_entry: Config entry
        vehicle_vin: Optional VIN to check specific vehicle

    Returns:
        Battery level as percentage (0-100), or None if not found
    """
    from ..const import (
        DOMAIN,
        CONF_TESLA_BLE_ENTITY_PREFIX,
        DEFAULT_TESLA_BLE_ENTITY_PREFIX,
    )
    from homeassistant.helpers import entity_registry as er, device_registry as dr

    # Method 1: Tesla BLE (only for non-VIN-specific queries)
    if vehicle_vin is None:
        config = dict(config_entry.options) if config_entry else {}
        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
        ble_battery_entity = f"sensor.{ble_prefix}_battery_level"
        ble_state = hass.states.get(ble_battery_entity)

        if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
            try:
                return float(ble_state.state)
            except (ValueError, TypeError):
                pass

    # Method 2: Check Tesla Fleet/Teslemetry entities
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)

    tesla_integrations = TESLA_INTEGRATIONS

    for device in device_registry.devices.values():
        is_tesla_vehicle = False
        device_vin: Optional[str] = None
        for identifier in device.identifiers:
            if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                id_str = str(identifier[1])
                if len(id_str) == 17 and not id_str.isdigit():
                    is_tesla_vehicle = True
                    device_vin = id_str
                    break

        if not is_tesla_vehicle:
            continue

        # If specific VIN requested, skip other vehicles
        if vehicle_vin is not None and device_vin != vehicle_vin:
            continue

        for entity in entity_registry.entities.values():
            if entity.device_id != device.id:
                continue

            entity_id = entity.entity_id
            entity_id_lower = entity_id.lower()

            # Look for battery level sensor
            if entity_id.startswith("sensor.") and (
                "battery_level" in entity_id_lower or
                "battery" in entity_id_lower and "level" in entity_id_lower or
                "state_of_charge" in entity_id_lower or
                "_soc" in entity_id_lower
            ):
                state = hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", "None", None):
                    try:
                        battery_level = float(state.state)
                        _LOGGER.debug(f"Found EV battery level from {entity_id} (VIN: {device_vin}): {battery_level}%")
                        return battery_level
                    except (ValueError, TypeError):
                        continue

    return None


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

        # Amber/Flow Power: Use Amber API for dynamic wholesale pricing (changes every 5 mins)
        if electricity_provider in ("amber", "flow_power"):
            amber_forecast = await self._get_amber_forecast(hours)
            if amber_forecast:
                return amber_forecast

        # Globird/AEMO VPP: Use Tesla tariff TOU schedule (fixed rates per period)
        elif electricity_provider in ("globird", "aemo_vpp"):
            tariff_forecast = await self._get_tariff_forecast(hours)
            if tariff_forecast:
                return tariff_forecast

        # Try Sigenergy tariff if available (for Sigenergy users with Amber)
        sigenergy_forecast = await self._get_sigenergy_tariff_forecast(hours)
        if sigenergy_forecast:
            return sigenergy_forecast

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
        """Get forecast from tariff schedule (Tesla tariff or custom tariff for Globird users)."""
        try:
            from ..const import DOMAIN

            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            tariff_schedule = entry_data.get("tariff_schedule", {})

            # If no tariff_schedule, try custom_tariff from automation_store
            if not tariff_schedule:
                automation_store = entry_data.get("automation_store")
                if automation_store:
                    custom_tariff = automation_store.get_custom_tariff()
                    if custom_tariff:
                        # Convert custom_tariff to tariff_schedule format
                        from .. import convert_custom_tariff_to_schedule
                        tariff_schedule = convert_custom_tariff_to_schedule(custom_tariff)
                        _LOGGER.debug(f"Using custom tariff for forecast: {custom_tariff.get('name')}")

            if not tariff_schedule:
                return None

            # Get rates and TOU schedule
            buy_rates = tariff_schedule.get("buy_rates", {})
            sell_rates = tariff_schedule.get("sell_rates", {})
            tou_periods = tariff_schedule.get("tou_periods", {})
            current_season = tariff_schedule.get("current_season", "Summer")

            if not buy_rates:
                _LOGGER.debug("No buy_rates in tariff schedule")
                return None

            _LOGGER.debug(f"Tariff forecast using rates: {buy_rates}, TOU periods: {list(tou_periods.keys())}")

            forecasts = []
            now = datetime.now()

            for h in range(hours):
                hour_dt = now + timedelta(hours=h)
                hour = hour_dt.hour
                dow = hour_dt.weekday()
                tesla_dow = (dow + 1) % 7  # Convert Python dow to Tesla dow (0=Sunday)

                # Find the TOU period for this hour using the actual schedule
                period_type = self._find_tou_period(tou_periods, hour, tesla_dow)

                # Get rate for this period - try exact match, then common variations
                import_rate = None
                for rate_key in [period_type, period_type.replace("_", ""), "ALL"]:
                    if rate_key in buy_rates:
                        import_rate = buy_rates[rate_key]
                        break

                if import_rate is None:
                    # Still not found - use first available rate
                    import_rate = next(iter(buy_rates.values()), 0.30)

                export_rate = None
                for rate_key in [period_type, period_type.replace("_", ""), "ALL"]:
                    if rate_key in sell_rates:
                        export_rate = sell_rates[rate_key]
                        break

                if export_rate is None:
                    export_rate = next(iter(sell_rates.values()), 0)

                # Convert to cents if in dollars (rates < 1 are likely $/kWh)
                import_cents = import_rate * 100 if import_rate < 1 else import_rate
                export_cents = export_rate * 100 if export_rate < 1 else export_rate

                # Determine display period name
                period_lower = period_type.lower()
                if "off" in period_lower or "super" in period_lower:
                    period = "offpeak"
                elif "on" in period_lower or "peak" in period_lower:
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
                # Log sample prices for debugging
                _LOGGER.info(
                    f"Tariff forecast: {len(forecasts)} hours, "
                    f"prices: {forecasts[0].import_cents:.1f}c now, "
                    f"{forecasts[min(3, len(forecasts)-1)].import_cents:.1f}c in 3h, "
                    f"{forecasts[min(12, len(forecasts)-1)].import_cents:.1f}c in 12h"
                )

                # Log any free/cheap periods found
                free_periods = [(f.hour, f.import_cents, f.period) for f in forecasts if f.import_cents <= 0]
                cheap_periods = [(f.hour, f.import_cents, f.period) for f in forecasts if 0 < f.import_cents <= 10]
                if free_periods:
                    _LOGGER.info(f"⚡ Found {len(free_periods)} FREE periods (0c): {[f[0][11:16] for f in free_periods[:5]]}")
                if cheap_periods:
                    _LOGGER.info(f"💰 Found {len(cheap_periods)} cheap periods (≤10c): {[(f[0][11:16], f'{f[1]:.0f}c') for f in cheap_periods[:5]]}")

            return forecasts

        except Exception as e:
            _LOGGER.warning(f"Could not get tariff forecast: {e}")
            return None

    async def _get_sigenergy_tariff_forecast(self, hours: int) -> Optional[List[PriceForecast]]:
        """Get forecast from Sigenergy tariff schedule (for Sigenergy users with Amber).

        Sigenergy tariff is stored as list of 30-min slots:
        {"buy_prices": [{"timeRange": "10:00-10:30", "price": 25.0}, ...]}
        """
        try:
            from ..const import DOMAIN

            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            sigenergy_tariff = entry_data.get("sigenergy_tariff", {})

            if not sigenergy_tariff:
                return None

            buy_prices = sigenergy_tariff.get("buy_prices", [])
            sell_prices = sigenergy_tariff.get("sell_prices", [])

            if not buy_prices:
                return None

            # Convert time slot prices to dict for fast lookup
            # Format: {("10", "00"): 25.0, ("10", "30"): 28.0, ...}
            buy_price_map = {}
            sell_price_map = {}

            for slot in buy_prices:
                time_range = slot.get("timeRange", "")
                if "-" in time_range:
                    start_time = time_range.split("-")[0]
                    if ":" in start_time:
                        h, m = start_time.split(":")
                        buy_price_map[(h, m)] = slot.get("price", 30.0)

            for slot in sell_prices:
                time_range = slot.get("timeRange", "")
                if "-" in time_range:
                    start_time = time_range.split("-")[0]
                    if ":" in start_time:
                        h, m = start_time.split(":")
                        sell_price_map[(h, m)] = slot.get("price", 8.0)

            # Generate hourly forecasts
            forecasts = []
            now = datetime.now()

            for h in range(hours):
                hour_dt = now + timedelta(hours=h)
                hour_str = f"{hour_dt.hour:02d}"

                # Get price for :00 slot (use as representative for the hour)
                import_cents = buy_price_map.get((hour_str, "00"), 30.0)
                export_cents = sell_price_map.get((hour_str, "00"), 8.0)

                # Also check :30 slot and use average if both exist
                import_30 = buy_price_map.get((hour_str, "30"))
                export_30 = sell_price_map.get((hour_str, "30"))
                if import_30 is not None:
                    import_cents = (import_cents + import_30) / 2
                if export_30 is not None:
                    export_cents = (export_cents + export_30) / 2

                # Determine period based on price
                if import_cents <= 0:
                    period = "super_offpeak"
                elif import_cents < 15:
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
                _LOGGER.info(f"Got {len(forecasts)} hours of Sigenergy tariff forecast")

            return forecasts if forecasts else None

        except Exception as e:
            _LOGGER.warning(f"Could not get Sigenergy tariff forecast: {e}")
            return None

    def _find_tou_period(self, tou_periods: dict, hour: int, tesla_dow: int) -> str:
        """
        Find the TOU period for a given hour and day of week.

        Args:
            tou_periods: Dict of period_name -> list of time ranges
            hour: Hour of day (0-23)
            tesla_dow: Day of week in Tesla format (0=Sunday)

        Returns:
            Period name (e.g., 'ON_PEAK', 'OFF_PEAK', 'SUPER_OFF_PEAK')
        """
        # Check periods in priority order to handle overlaps correctly
        # SUPER_OFF_PEAK must be checked before OFF_PEAK since OFF_PEAK may include hours
        # that overlap with SUPER_OFF_PEAK
        period_priority = ["SUPER_OFF_PEAK", "ON_PEAK", "PEAK", "PARTIAL_PEAK", "SHOULDER", "OFF_PEAK"]
        for period_name in period_priority:
            if period_name not in tou_periods:
                continue
            period_data = tou_periods[period_name]
            periods_list = period_data if isinstance(period_data, list) else []
            for period in periods_list:
                from_dow = period.get("fromDayOfWeek", 0)
                to_dow = period.get("toDayOfWeek", 6)
                from_hour = period.get("fromHour", 0)
                to_hour = period.get("toHour", 24)

                # Check day of week
                if from_dow <= tesla_dow <= to_dow:
                    # Check time - handle overnight periods (e.g., 21:00 to 10:00)
                    if from_hour <= to_hour:
                        # Normal period (e.g., 10:00 to 14:00)
                        if from_hour <= hour < to_hour:
                            _LOGGER.debug(
                                f"TOU match: hour={hour}, dow={tesla_dow} -> {period_name} "
                                f"(from_hour={from_hour}, to_hour={to_hour})"
                            )
                            return period_name
                    else:
                        # Overnight period (e.g., 21:00 to 10:00)
                        if hour >= from_hour or hour < to_hour:
                            return period_name

        # Default fallback - log when we fall back to ALL
        _LOGGER.debug(f"TOU fallback: hour={hour}, dow={tesla_dow} -> ALL (no match found)")
        return "ALL"

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

    def __init__(self, hass, config_entry, battery_schedule_getter=None, grid_capacity_kw: float = 7.4):
        """Initialize the planner.

        Args:
            hass: Home Assistant instance
            config_entry: Config entry for this integration
            battery_schedule_getter: Optional callback to get battery optimization schedule.
                                    Used to calculate available power for EV when battery
                                    is also charging (dynamic power sharing).
            grid_capacity_kw: Total grid import capacity in kW (default 7.4kW = 32A single phase)
        """
        self.hass = hass
        self.config_entry = config_entry
        self.surplus_forecaster = SurplusForecaster(hass)
        self.price_forecaster = PriceForecaster(hass, config_entry)
        self._get_battery_schedule = battery_schedule_getter
        self._grid_capacity_kw = grid_capacity_kw

    async def _get_battery_power_schedule(self, hours: int = 24) -> Dict[str, float]:
        """Get battery power usage per hour from optimizer schedule.

        Returns dict of {hour_iso: power_kw} for battery charging periods.
        This allows EV to use remaining grid capacity during shared charging windows.
        """
        if not self._get_battery_schedule:
            return {}

        try:
            schedule = self._get_battery_schedule()
            if hasattr(schedule, '__await__'):
                schedule = await schedule

            if not schedule:
                return {}

            # Build hour -> power mapping
            battery_power = {}

            for action in schedule:
                if isinstance(action, dict):
                    ts_str = action.get("timestamp")
                    action_type = action.get("action")
                    power_w = action.get("power_w", 0)
                else:
                    ts_str = getattr(action, "timestamp", None)
                    action_type = getattr(action, "action", None)
                    power_w = getattr(action, "power_w", 0)

                if not ts_str or action_type != "charge":
                    continue

                # Parse timestamp and round to hour
                if isinstance(ts_str, str):
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                else:
                    ts = ts_str

                hour_key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
                power_kw = power_w / 1000 if power_w > 100 else power_w  # Handle W vs kW

                # Take max power for each hour (conservative estimate)
                if hour_key in battery_power:
                    battery_power[hour_key] = max(battery_power[hour_key], power_kw)
                else:
                    battery_power[hour_key] = power_kw

            return battery_power

        except Exception as e:
            _LOGGER.debug(f"Error getting battery schedule: {e}")
            return {}

    def _get_available_ev_power(
        self,
        hour: str,
        charger_max_kw: float,
        battery_power_schedule: Dict[str, float],
        solar_surplus_kw: float = 0,
    ) -> float:
        """Calculate available power for EV charging at a given hour.

        Dynamic power sharing: EV gets remaining capacity after battery.
        During solar surplus, both can charge at full rate.

        Args:
            hour: Hour in ISO format
            charger_max_kw: Maximum EV charger power
            battery_power_schedule: Dict of hour -> battery charging power
            solar_surplus_kw: Available solar surplus (can exceed grid capacity)

        Returns:
            Available power for EV in kW
        """
        battery_power = battery_power_schedule.get(hour, 0)

        if solar_surplus_kw > 0:
            # Solar surplus available - EV can use surplus + remaining grid
            # Solar powers both battery and EV, only grid import is limited
            grid_needed_by_battery = max(0, battery_power - solar_surplus_kw)
            available_grid = self._grid_capacity_kw - grid_needed_by_battery
            available_solar = max(0, solar_surplus_kw - battery_power)
            total_available = available_grid + available_solar
        else:
            # No solar - share grid capacity with battery
            total_available = self._grid_capacity_kw - battery_power

        # Clamp to charger limits and minimum charging threshold
        available = min(charger_max_kw, max(0, total_available))

        # Minimum 1.4kW (6A) to actually charge
        if available < 1.4:
            return 0

        return available

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
            now = datetime.now()
            # Convert target_time to naive local time for comparison
            target_time_local = target_time
            if target_time.tzinfo is not None:
                try:
                    local_tz = datetime.now().astimezone().tzinfo
                    target_time_local = target_time.astimezone(local_tz).replace(tzinfo=None)
                except Exception:
                    target_time_local = target_time.replace(tzinfo=None)
            # Exact hours available until departure — ceil to include the partial final hour
            import math
            hours_available = max(1, math.ceil((target_time_local - now).total_seconds() / 3600))
        else:
            hours_available = 24

        # Get forecasts
        surplus_forecast = await self.surplus_forecaster.forecast_surplus(hours_available)
        price_forecast = await self.price_forecaster.get_price_forecast(hours_available)

        # Get battery power schedule for dynamic power sharing
        # EV uses remaining grid capacity when battery is also charging
        # Both charge during cheap/solar periods - we just share the available power
        battery_power_schedule = await self._get_battery_power_schedule(hours_available)
        if battery_power_schedule:
            _LOGGER.debug(
                f"Battery charging in {len(battery_power_schedule)} hours - "
                f"EV will share grid capacity (max {self._grid_capacity_kw}kW)"
            )

        # Create plan based on priority
        if priority == ChargingPriority.SOLAR_ONLY:
            plan = await self._plan_solar_only(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast,
                battery_power_schedule=battery_power_schedule,
            )
        elif priority == ChargingPriority.SOLAR_PREFERRED:
            plan = await self._plan_solar_preferred(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
                battery_power_schedule=battery_power_schedule,
            )
        elif priority == ChargingPriority.COST_OPTIMIZED:
            plan = await self._plan_cost_optimized(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
                battery_power_schedule=battery_power_schedule,
            )
        else:  # TIME_CRITICAL
            plan = await self._plan_time_critical(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
                battery_power_schedule=battery_power_schedule,
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
        battery_power_schedule: Dict[str, float] = None,
    ) -> ChargingPlan:
        """Plan charging using only solar surplus with dynamic power sharing."""
        windows = []
        energy_allocated = 0
        total_confidence = 0
        battery_power_schedule = battery_power_schedule or {}

        for forecast in surplus_forecast:
            if energy_allocated >= energy_needed_kwh:
                break

            if forecast.surplus_kw >= 1.0:  # Minimum 1kW to charge
                hour_dt = datetime.fromisoformat(forecast.hour)
                hour_key = hour_dt.replace(minute=0, second=0, microsecond=0).isoformat()

                # Dynamic power sharing: calculate available power for EV
                # Solar surplus can power both battery and EV simultaneously
                available_power = self._get_available_ev_power(
                    hour_key,
                    charger_power_kw,
                    battery_power_schedule,
                    solar_surplus_kw=forecast.surplus_kw,
                )

                if available_power < 1.4:  # Minimum 6A to charge
                    continue

                energy_this_hour = available_power  # kWh (1 hour)

                # Don't over-allocate
                energy_this_hour = min(energy_this_hour, energy_needed_kwh - energy_allocated)

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
        battery_power_schedule: Dict[str, float] = None,
    ) -> ChargingPlan:
        """Plan charging preferring solar, falling back to offpeak grid with dynamic power sharing."""
        windows = []
        solar_energy = 0
        grid_energy = 0
        total_cost = 0
        total_confidence = 0
        battery_power_schedule = battery_power_schedule or {}

        # First pass: allocate solar
        for forecast in surplus_forecast:
            if solar_energy + grid_energy >= energy_needed_kwh:
                break

            if forecast.surplus_kw >= 1.0:
                hour_dt = datetime.fromisoformat(forecast.hour)
                hour_key = hour_dt.replace(minute=0, second=0, microsecond=0).isoformat()

                # Dynamic power sharing with battery
                available_power = self._get_available_ev_power(
                    hour_key,
                    charger_power_kw,
                    battery_power_schedule,
                    solar_surplus_kw=forecast.surplus_kw,
                )

                if available_power < 1.4:
                    continue

                energy_this_hour = min(available_power, energy_needed_kwh - solar_energy - grid_energy)
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

        # Second pass: fill with cheapest grid hours if needed
        # Sort by price (cheapest first) to prefer offpeak/cheap hours
        # Cheap hours are when battery also charges - use dynamic power sharing
        remaining_energy = energy_needed_kwh - solar_energy
        if remaining_energy > 0 and price_forecast:
            # Sort all hours by price (cheapest first)
            sorted_by_price = sorted(price_forecast, key=lambda p: p.import_cents)

            for price_data in sorted_by_price:
                if grid_energy >= remaining_energy:
                    break

                # Check if this hour is already covered by solar
                already_covered = any(
                    w.start_time == price_data.hour for w in windows
                )
                if already_covered:
                    continue

                hour_dt = datetime.fromisoformat(price_data.hour)
                hour_key = hour_dt.replace(minute=0, second=0, microsecond=0).isoformat()

                # Dynamic power sharing: cheap hours = battery charging too
                available_power = self._get_available_ev_power(
                    hour_key,
                    charger_power_kw,
                    battery_power_schedule,
                    solar_surplus_kw=0,  # No solar during grid-only hours
                )

                if available_power < 1.4:
                    continue  # Not enough capacity, try next hour

                energy_this_hour = min(available_power, remaining_energy - grid_energy)
                end_dt = hour_dt + timedelta(hours=1)

                # Label source based on period type
                source = f"grid_{price_data.period}" if price_data.period else "grid_cheap"
                reason = "offpeak_rate" if price_data.period == "offpeak" else "cheap_rate"

                windows.append(PlannedChargingWindow(
                    start_time=price_data.hour,
                    end_time=end_dt.isoformat(),
                    source=source,
                    estimated_power_kw=available_power,
                    estimated_energy_kwh=energy_this_hour,
                    price_cents_kwh=price_data.import_cents,
                    reason=reason,
                ))

                grid_energy += energy_this_hour
                total_cost += energy_this_hour * price_data.import_cents
                total_confidence += 0.9  # Grid is reliable

        # Sort windows by time
        windows.sort(key=lambda w: w.start_time)

        # Check if we can meet target
        total_energy = solar_energy + grid_energy
        can_meet = total_energy >= energy_needed_kwh * 0.9

        # Generate warning if target can't be met
        warning = None
        if not can_meet:
            if not windows:
                warning = "No charging windows available - check price/solar forecast"
            elif solar_energy == 0 and grid_energy == 0:
                warning = "No solar or grid windows could be planned"
            else:
                warning = f"Planned {total_energy:.1f}kWh but need {energy_needed_kwh:.1f}kWh"

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
            warning=warning,
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
        battery_power_schedule: Dict[str, float] = None,
    ) -> ChargingPlan:
        """
        Plan charging to minimize cost while meeting departure deadline.

        Strategy:
        1. Get all available charging windows before departure time
        2. Sort by price (cheapest first), with solar surplus as free (0 cost)
        3. Select cheapest windows until energy requirement is met
        4. If deadline is tight, prioritize meeting deadline over cost
        5. Dynamic power sharing: adjust EV amps based on battery charge rate

        Example scenarios:
        - Plugged in at 11am with 1c/kWh price -> charge immediately
        - Arrive home 6pm at 58c/kWh, depart 6am with 15-20c overnight -> wait for cheap overnight
        - Battery charging at 5kW during cheap period -> EV charges at reduced rate
        """
        now = datetime.now()
        battery_power_schedule = battery_power_schedule or {}

        # Convert target_time to naive local time for comparison
        # Price forecast hours are stored as naive local time strings
        target_time_local = None
        if target_time:
            if target_time.tzinfo is not None:
                # Convert UTC target_time to local time, then strip timezone
                try:
                    import zoneinfo
                    # Try to get local timezone
                    local_tz = datetime.now().astimezone().tzinfo
                    target_time_local = target_time.astimezone(local_tz).replace(tzinfo=None)
                except Exception:
                    # Fallback: assume price hours are in same tz as target, strip both
                    target_time_local = target_time.replace(tzinfo=None)
            else:
                target_time_local = target_time

        _LOGGER.info(
            f"Planning cost-optimized charging: need {energy_needed_kwh:.1f}kWh, "
            f"charger={charger_power_kw}kW, target_time={target_time} (local: {target_time_local})"
        )

        # Build charging options from price forecast (within deadline)
        charging_options = []

        for i, price in enumerate(price_forecast):
            try:
                hour_dt = datetime.fromisoformat(price.hour)
                # Price hours are naive local time - strip any timezone to ensure naive comparison
                if hour_dt.tzinfo is not None:
                    hour_dt = hour_dt.replace(tzinfo=None)
            except:
                continue

            # Skip if past departure time (compare naive local times)
            if target_time_local and hour_dt >= target_time_local:
                continue

            # Skip if in the past
            if hour_dt < now - timedelta(hours=1):
                continue

            # Calculate usable fraction of this hour (clamp to departure and now)
            hour_end = hour_dt + timedelta(hours=1)
            if target_time_local and hour_end > target_time_local:
                hour_end = target_time_local
            usable_fraction = (hour_end - max(hour_dt, now)).total_seconds() / 3600
            usable_fraction = max(0.0, min(1.0, usable_fraction))
            if usable_fraction < 0.1:
                continue  # Less than 6 minutes usable — skip

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
                    "usable_fraction": usable_fraction,
                })

            # Grid option
            # When grid is free (0c) or negative, use full charger power - don't reduce for solar
            # Solar forecast is uncertain, but free grid is guaranteed
            if price.import_cents <= 0:
                grid_power = charger_power_kw  # Full power when grid is free/negative
            else:
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
                    "usable_fraction": usable_fraction,
                })

        # Log available options
        if charging_options:
            prices = [opt["cost_cents"] for opt in charging_options]
            grid_options = [opt for opt in charging_options if opt["source"].startswith("grid")]
            negative_price_windows = [opt for opt in grid_options if opt["cost_cents"] < 0]
            free_grid_windows = [opt for opt in grid_options if opt["cost_cents"] == 0]

            _LOGGER.info(
                f"Found {len(charging_options)} charging options, "
                f"prices range: {min(prices):.1f}c - {max(prices):.1f}c"
            )

            # Log special pricing conditions
            if negative_price_windows:
                _LOGGER.info(
                    f"💰 {len(negative_price_windows)} negative price windows available "
                    f"(get PAID to charge!) - cheapest: {min(opt['cost_cents'] for opt in negative_price_windows):.1f}c/kWh"
                )
            if free_grid_windows:
                _LOGGER.info(
                    f"⚡ {len(free_grid_windows)} free grid windows available (0c/kWh) - "
                    f"preferring over solar forecast"
                )

        # Sort by cost (cheapest first)
        # Secondary sort by time to prefer earlier slots at same price
        # Third: when grid is free/negative, prefer it over solar (grid is guaranteed, solar is forecast)
        def sort_key(x):
            cost = x["cost_cents"]
            time = x["hour_dt"]
            # When cost is <= 0 (free or negative), prefer grid over solar
            # 0 = grid (preferred), 1 = solar
            source_pref = 0 if x["source"].startswith("grid") and cost <= 0 else 1
            return (cost, time, source_pref)

        charging_options.sort(key=sort_key)

        # Log top 5 cheapest options
        for i, opt in enumerate(charging_options[:5]):
            price_note = ""
            if opt["cost_cents"] < 0:
                price_note = " 💰 GET PAID"
            elif opt["cost_cents"] == 0 and opt["source"].startswith("grid"):
                price_note = " ⚡ FREE"
            _LOGGER.debug(
                f"  Option {i+1}: {opt['hour_dt'].strftime('%H:%M')} - "
                f"{opt['cost_cents']:.1f}c/kWh ({opt['source']}){price_note}"
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

            usable = option.get("usable_fraction", 1.0)
            energy_this_hour = min(option["power_kw"] * usable, energy_needed_kwh - energy_allocated)
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
        battery_power_schedule: Dict[str, float] = None,
    ) -> ChargingPlan:
        """Plan charging to meet deadline, minimizing cost as secondary goal."""
        battery_power_schedule = battery_power_schedule or {}

        if not target_time:
            # No deadline, use cost-optimized
            return await self._plan_cost_optimized(
                vehicle_id, current_soc, target_soc, target_time,
                energy_needed_kwh, charger_power_kw,
                surplus_forecast, price_forecast,
                battery_power_schedule=battery_power_schedule,
            )

        # Calculate minimum hours needed (0.85 efficiency: AC-DC losses, ramp-up, thermal)
        hours_needed = energy_needed_kwh / (charger_power_kw * 0.85)
        now = datetime.now()

        # Convert target_time to naive local time for comparison
        # Price forecast hours are stored as naive local time strings
        target_time_local = target_time
        if target_time.tzinfo is not None:
            try:
                local_tz = datetime.now().astimezone().tzinfo
                target_time_local = target_time.astimezone(local_tz).replace(tzinfo=None)
            except Exception:
                target_time_local = target_time.replace(tzinfo=None)

        # Exact hours available until departure — no padding
        import math
        hours_available = max(1, math.ceil((target_time_local - now).total_seconds() / 3600))

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
            # Price hours are naive local time - strip any timezone
            if hour_dt.tzinfo is not None:
                hour_dt = hour_dt.replace(tzinfo=None)

            if target_time_local and hour_dt >= target_time_local:
                continue  # Skip hours that start at or after deadline

            # Calculate usable fraction of this hour (clamp end to departure)
            end_dt = hour_dt + timedelta(hours=1)
            if target_time_local and end_dt > target_time_local:
                end_dt = target_time_local
            usable_fraction = (end_dt - max(hour_dt, now)).total_seconds() / 3600
            usable_fraction = max(0.0, min(1.0, usable_fraction))
            if usable_fraction < 0.1:
                continue  # Less than 6 minutes usable — skip

            # Use whatever is available, scaled by usable fraction of the hour
            if surplus.surplus_kw >= 1.0:
                # Prefer solar
                energy_this_hour = min(surplus.surplus_kw, charger_power_kw) * usable_fraction
                source = "solar_surplus"
                cost = 0
                solar_energy += min(energy_this_hour, energy_needed_kwh - energy_allocated)
            else:
                # Use grid
                energy_this_hour = charger_power_kw * usable_fraction
                source = f"grid_{price.period}"
                cost = price.import_cents
                grid_energy += min(energy_this_hour, energy_needed_kwh - energy_allocated)

            energy_this_hour = min(energy_this_hour, energy_needed_kwh - energy_allocated)

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
        is_time_critical: bool = False,
    ) -> Tuple[bool, str, str]:
        """
        Real-time decision: should we charge right now?

        Strategy:
        1. For time_critical mode: prioritize meeting deadline over all else
        2. For other modes: respect home battery priority (min SoC)
        3. If in a planned window from cost-optimized plan, charge
        4. Opportunistic: charge on solar surplus (free)
        5. Opportunistic: charge if current price is very cheap (< plan avg or < 10c)
        6. Otherwise wait for planned windows or better prices

        Args:
            vehicle_id: Vehicle identifier
            plan: Current charging plan
            current_surplus_kw: Current solar surplus
            current_price_cents: Current import price
            battery_soc: Current home battery SoC
            min_battery_soc: Minimum home battery SoC before EV charging
            is_time_critical: If True, meeting deadline takes priority over battery/price

        Returns:
            Tuple of (should_charge, reason, source)
        """
        now = datetime.now()

        # For time_critical mode with a deadline, check if we need to charge NOW to meet target
        if is_time_critical and plan.target_time and not plan.can_meet_target:
            # We're behind schedule - charge immediately regardless of price/battery
            return True, f"Must charge to meet deadline (behind schedule)", "grid_deadline"

        if is_time_critical and plan.target_time:
            # Check if we're in a critical window where we MUST charge to meet deadline
            try:
                target_dt = datetime.fromisoformat(plan.target_time)
                if target_dt.tzinfo is not None:
                    # Convert to local naive time
                    local_tz = datetime.now().astimezone().tzinfo
                    target_dt = target_dt.astimezone(local_tz).replace(tzinfo=None)

                hours_remaining = (target_dt - now).total_seconds() / 3600
                hours_needed = plan.energy_needed_kwh / 7.0  # Assume ~7kW charger

                # If we need to charge continuously to meet target, do it now
                if hours_remaining <= hours_needed * 1.2:  # 20% buffer
                    return True, f"Critical: {hours_remaining:.1f}h left, need {hours_needed:.1f}h", "grid_deadline"
            except Exception as e:
                _LOGGER.debug(f"Error checking time_critical deadline: {e}")

        # Note: min_battery_soc is used to prevent battery DISCHARGE during surplus
        # calculation, but does NOT block charging from solar/grid.
        # The Powerwall's own backup reserve handles discharge protection.

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
    departure_time: Optional[str] = None  # HH:MM format (legacy, kept for backward compat)
    departure_days: List[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon-Fri (legacy)
    departure_times: Dict[int, str] = field(default_factory=dict)  # {day_index: "HH:MM"} e.g. {0: "07:30", 4: "07:30"}

    # Priority mode
    priority: ChargingPriority = ChargingPriority.COST_OPTIMIZED
    departure_priorities: Dict[int, str] = field(default_factory=dict)  # {day_index: "priority"} e.g. {0: "time_critical", 5: "solar_only"}

    # Battery constraints
    min_battery_to_start: int = 20  # Don't START EV charging unless home battery >= this %
    consume_battery_level: int = 0  # Discharge home battery to X% for EV (0 = disabled)
    stop_at_battery_floor: bool = True  # When battery hits consume level, stop EV (no grid fallback)
    limit_grid_import: bool = False  # Dynamically adjust EV charge amps to match inverter capacity
    max_grid_price_cents: float = 25.0  # Don't charge from grid above this price (backend only, not in mobile UI)

    # Per-day constraint overrides (days without entries fall back to global settings above)
    departure_min_battery_to_start: Dict[int, int] = field(default_factory=dict)  # {day_index: percent}
    departure_consume_battery_level: Dict[int, int] = field(default_factory=dict)  # {day_index: percent}
    departure_stop_at_battery_floor: Dict[int, bool] = field(default_factory=dict)  # {day_index: True/False}
    departure_limit_grid_import: Dict[int, bool] = field(default_factory=dict)  # {day_index: True/False}

    # Charger settings
    charger_type: str = "tesla"  # tesla, ocpp, generic
    min_charge_amps: int = 5  # Tesla minimum is 5A
    max_charge_amps: int = 32
    voltage: int = 230  # Australia standard voltage
    phases: int = 1  # 1 for single phase, 3 for three phase

    def get_min_surplus_kw(self) -> float:
        """Calculate minimum surplus based on charger electrical requirements.

        Tesla requires minimum 5A to charge:
        - Single phase: 5A × 230V = 1.15kW
        - Three phase: 5A × 230V × 3 = 3.45kW
        """
        return (self.min_charge_amps * self.voltage * self.phases) / 1000

    def get_effective_priority(self, weekday: int) -> "ChargingPriority":
        """Get the effective priority for a given weekday, falling back to global priority."""
        if weekday in self.departure_priorities:
            try:
                return ChargingPriority(self.departure_priorities[weekday])
            except ValueError:
                pass
        return self.priority

    def get_effective_limit_grid_import(self, weekday: int) -> bool:
        """Get the effective limit_grid_import for a given weekday."""
        if weekday in self.departure_limit_grid_import:
            return self.departure_limit_grid_import[weekday]
        return self.limit_grid_import

    def get_effective_min_battery_to_start(self, weekday: int) -> int:
        """Get the effective min_battery_to_start for a given weekday."""
        if weekday in self.departure_min_battery_to_start:
            return self.departure_min_battery_to_start[weekday]
        return self.min_battery_to_start

    def get_effective_consume_battery_level(self, weekday: int) -> int:
        """Get the effective consume_battery_level for a given weekday."""
        if weekday in self.departure_consume_battery_level:
            return self.departure_consume_battery_level[weekday]
        return self.consume_battery_level

    def get_effective_stop_at_battery_floor(self, weekday: int) -> bool:
        """Get the effective stop_at_battery_floor for a given weekday."""
        if weekday in self.departure_stop_at_battery_floor:
            return self.departure_stop_at_battery_floor[weekday]
        return self.stop_at_battery_floor

    def get_effective_max_grid_price(self, weekday: int) -> float:
        """Get the effective max_grid_price_cents for a given weekday."""
        return self.max_grid_price_cents

    # Optional entity overrides for generic chargers
    charger_switch_entity: Optional[str] = None
    charger_amps_entity: Optional[str] = None
    ocpp_charger_id: Optional[int] = None

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        # Derive legacy fields from departure_times for backward compat
        legacy_departure_time = None
        legacy_departure_days = []
        if self.departure_times:
            legacy_departure_days = sorted(self.departure_times.keys())
            # Use first found time as legacy departure_time
            legacy_departure_time = next(iter(self.departure_times.values()), None)
        return {
            "enabled": self.enabled,
            "vehicle_id": self.vehicle_id,
            "display_name": self.display_name,
            "target_soc": self.target_soc,
            "departure_time": legacy_departure_time,
            "departure_days": legacy_departure_days,
            "departure_times": {str(k): v for k, v in self.departure_times.items()},
            "departure_priorities": {str(k): v for k, v in self.departure_priorities.items()},
            "departure_min_battery_to_start": {str(k): v for k, v in self.departure_min_battery_to_start.items()},
            "departure_consume_battery_level": {str(k): v for k, v in self.departure_consume_battery_level.items()},
            "departure_stop_at_battery_floor": {str(k): v for k, v in self.departure_stop_at_battery_floor.items()},
            "departure_limit_grid_import": {str(k): v for k, v in self.departure_limit_grid_import.items()},
            "priority": self.priority.value,
            "min_battery_to_start": self.min_battery_to_start,
            "consume_battery_level": self.consume_battery_level,
            "stop_at_battery_floor": self.stop_at_battery_floor,
            "limit_grid_import": self.limit_grid_import,
            "max_grid_price_cents": self.max_grid_price_cents,
            # Backward compat aliases for older mobile clients
            "home_battery_minimum": self.min_battery_to_start,
            "no_grid_import": self.limit_grid_import,
            "charger_type": self.charger_type,
            "min_charge_amps": self.min_charge_amps,
            "max_charge_amps": self.max_charge_amps,
            "voltage": self.voltage,
            "phases": self.phases,
            "min_surplus_kw": self.get_min_surplus_kw(),  # Calculated from phases/voltage/amps
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

        # Backward compatibility: map old field names to new names
        # min_battery_soc → home_battery_minimum → min_battery_to_start
        old_min_battery = data.get("min_battery_soc", 20)
        legacy_home_battery_min = data.get("home_battery_minimum", old_min_battery if old_min_battery <= 30 else 20)
        min_battery_to_start = data.get("min_battery_to_start", legacy_home_battery_min)

        # no_grid_import → limit_grid_import
        legacy_no_grid = data.get("no_grid_import", False)
        limit_grid_import = data.get("limit_grid_import", legacy_no_grid)

        # New fields with defaults
        consume_battery_level = data.get("consume_battery_level", 0)
        stop_at_battery_floor = data.get("stop_at_battery_floor", True)

        # Handle departure_priorities (per-day strategy overrides)
        departure_priorities: Dict[int, str] = {}
        raw_departure_priorities = data.get("departure_priorities")
        if raw_departure_priorities and isinstance(raw_departure_priorities, dict):
            departure_priorities = {int(k): v for k, v in raw_departure_priorities.items()}

        # Handle per-day constraint overrides (new names, with backward compat from old names)
        departure_min_battery_to_start: Dict[int, int] = {}
        raw_dmbts = data.get("departure_min_battery_to_start") or data.get("departure_home_battery_min")
        if raw_dmbts and isinstance(raw_dmbts, dict):
            departure_min_battery_to_start = {int(k): int(v) for k, v in raw_dmbts.items()}

        departure_limit_grid_import: Dict[int, bool] = {}
        raw_dlgi = data.get("departure_limit_grid_import") or data.get("departure_no_grid_import")
        if raw_dlgi and isinstance(raw_dlgi, dict):
            departure_limit_grid_import = {int(k): bool(v) for k, v in raw_dlgi.items()}

        departure_consume_battery_level: Dict[int, int] = {}
        raw_dcbl = data.get("departure_consume_battery_level")
        if raw_dcbl and isinstance(raw_dcbl, dict):
            departure_consume_battery_level = {int(k): int(v) for k, v in raw_dcbl.items()}

        departure_stop_at_battery_floor: Dict[int, bool] = {}
        raw_dsabf = data.get("departure_stop_at_battery_floor")
        if raw_dsabf and isinstance(raw_dsabf, dict):
            departure_stop_at_battery_floor = {int(k): bool(v) for k, v in raw_dsabf.items()}

        # Handle departure_times migration from legacy format
        departure_times: Dict[int, str] = {}
        raw_departure_times = data.get("departure_times")
        if raw_departure_times and isinstance(raw_departure_times, dict):
            # New format: {"0": "07:30", "4": "07:30"} or {0: "07:30"}
            departure_times = {int(k): v for k, v in raw_departure_times.items()}
        else:
            # Legacy format: departure_time + departure_days → build departure_times dict
            legacy_time = data.get("departure_time")
            legacy_days = data.get("departure_days", [0, 1, 2, 3, 4])
            if legacy_time:
                departure_times = {day: legacy_time for day in legacy_days}

        return cls(
            enabled=data.get("enabled", False),
            vehicle_id=data.get("vehicle_id", "_default"),
            display_name=data.get("display_name", "EV"),
            target_soc=data.get("target_soc", 80),
            departure_time=data.get("departure_time"),
            departure_days=data.get("departure_days", [0, 1, 2, 3, 4]),
            departure_times=departure_times,
            departure_priorities=departure_priorities,
            departure_min_battery_to_start=departure_min_battery_to_start,
            departure_consume_battery_level=departure_consume_battery_level,
            departure_stop_at_battery_floor=departure_stop_at_battery_floor,
            departure_limit_grid_import=departure_limit_grid_import,
            priority=priority,
            min_battery_to_start=min_battery_to_start,
            consume_battery_level=consume_battery_level,
            stop_at_battery_floor=stop_at_battery_floor,
            limit_grid_import=limit_grid_import,
            max_grid_price_cents=data.get("max_grid_price_cents", 25.0),
            charger_type=data.get("charger_type", "tesla"),
            min_charge_amps=data.get("min_charge_amps", 5),
            max_charge_amps=data.get("max_charge_amps", 32),
            voltage=data.get("voltage", 230),
            phases=data.get("phases", 1),
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

    # Curtailment override management - track original export rule to restore after charging
    original_export_rule: Optional[str] = None
    curtailment_override_active: bool = False

    # Cached SoC - used when vehicle is asleep and can't report live SoC
    last_known_soc: Optional[int] = None
    last_soc_update: Optional[datetime] = None

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
            "original_export_rule": self.original_export_rule,
            "curtailment_override_active": self.curtailment_override_active,
            "last_known_soc": self.last_known_soc,
            "last_soc_update": self.last_soc_update.isoformat() if self.last_soc_update else None,
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

        # Cached SoC values per vehicle (persisted to storage)
        # Used when vehicle is asleep and can't report live SoC
        self._cached_soc: Dict[str, dict] = {}  # {vehicle_id: {"soc": int, "updated": isoformat}}

        # Store reference for saving cached SoC
        self._store = None
        self._last_cache_save: Optional[datetime] = None
        self._cache_save_interval = timedelta(minutes=5)  # Save cache every 5 minutes max

        # Plan regeneration interval (regenerate every 5 minutes to match Amber/AEMO pricing)
        self._plan_update_interval = timedelta(minutes=5)

        # Smart Optimization integration
        self._use_ml_optimization = False  # Set via settings

        # Variable charge rate tracking (per vehicle)
        self._current_charge_amps: Dict[str, int] = {}  # {vehicle_id: current_amps}
        self._charge_rate_change_threshold = 2  # Only change rate if diff >= 2 amps

    def _get_ml_ev_schedule(self, vehicle_id: str):
        """
        Get the optimization schedule for a vehicle if available.

        Returns:
            EVChargingSchedule or None if Smart Optimization is not enabled/available
        """
        if not self._use_ml_optimization:
            return None

        try:
            from ..const import DOMAIN

            # Get the optimization coordinator
            domain_data = self.hass.data.get(DOMAIN, {})
            entry_data = domain_data.get(self.config_entry.entry_id, {})
            opt_coordinator = entry_data.get("optimization_coordinator")

            if not opt_coordinator:
                return None

            # Check if EV integration is enabled in Smart Optimization
            if not getattr(opt_coordinator, '_enable_ev', False):
                return None

            # Get EV schedules from the optimization coordinator
            ev_schedules = getattr(opt_coordinator, '_ev_schedules', [])
            if not ev_schedules:
                return None

            # Find schedule for this vehicle
            for schedule in ev_schedules:
                if schedule.vehicle_id == vehicle_id and schedule.success:
                    return schedule

            return None

        except Exception as e:
            _LOGGER.debug(f"Error getting ML EV schedule: {e}")
            return None

    def set_use_ml_optimization(self, enabled: bool) -> None:
        """Enable or disable Smart Optimization for EV charging decisions."""
        self._use_ml_optimization = enabled
        _LOGGER.info(f"Smart Optimization for EV charging: {'enabled' if enabled else 'disabled'}")

    def _power_to_amps(self, power_w: float, voltage: int = 230, phases: int = 1) -> int:
        """
        Convert power in watts to charging amps.

        Args:
            power_w: Power in watts
            voltage: Voltage (default 230V for Australia)
            phases: Number of phases (1 or 3)

        Returns:
            Charging amps (clamped to 5-32A range)
        """
        if power_w <= 0:
            return 0

        # P = V * I * phases (for AC charging)
        amps = power_w / (voltage * phases)

        # Clamp to valid Tesla charging range (5A minimum, 32A typical max for home chargers)
        # Below 5A Tesla refuses to charge
        amps = max(5, min(32, int(amps)))

        return amps

    async def _set_vehicle_charge_rate(
        self,
        vehicle_id: str,
        power_w: float,
        settings: "AutoScheduleSettings",
    ) -> bool:
        """
        Set the charging rate for a vehicle based on target power.

        Supports:
        - Tesla Fleet API
        - Tesla BLE
        - Teslemetry

        Args:
            vehicle_id: Vehicle identifier (VIN)
            power_w: Target charging power in watts
            settings: Vehicle's auto-schedule settings

        Returns:
            True if charge rate was set successfully
        """
        from .actions import _action_set_ev_charging_amps

        # Get home power settings for voltage/phases
        home_config = await self._get_home_power_settings()
        phases = 1 if home_config.get("phase_type") == "single" else 3
        voltage = 230  # Australia standard

        # Convert power to amps
        target_amps = self._power_to_amps(power_w, voltage, phases)

        if target_amps == 0:
            return False

        # Check if rate change is significant enough
        current_amps = self._current_charge_amps.get(vehicle_id, 0)
        if abs(target_amps - current_amps) < self._charge_rate_change_threshold:
            _LOGGER.debug(
                f"Charge rate change too small ({current_amps}A → {target_amps}A), skipping"
            )
            return True  # Not an error, just no change needed

        # Set the charge rate
        params = {
            "vehicle_vin": vehicle_id,
            "amps": target_amps,
        }

        try:
            success = await _action_set_ev_charging_amps(
                self.hass, self.config_entry, params
            )

            if success:
                self._current_charge_amps[vehicle_id] = target_amps
                _LOGGER.info(
                    f"⚡ Variable charge rate: Set {vehicle_id} to {target_amps}A "
                    f"({power_w/1000:.1f}kW @ {voltage}V/{phases}ph)"
                )
                return True
            else:
                _LOGGER.warning(f"Failed to set charge rate for {vehicle_id}")
                return False

        except Exception as e:
            _LOGGER.error(f"Error setting charge rate for {vehicle_id}: {e}")
            return False

    async def load_settings(self, store) -> None:
        """Load settings from storage."""
        self._store = store  # Store reference for saving cached SoC later

        try:
            stored_data = await store.async_load() if hasattr(store, 'async_load') else {}
            if not stored_data:
                stored_data = {}

            auto_schedule_data = stored_data.get("auto_schedule_settings", {})

            for vehicle_id, settings_dict in auto_schedule_data.items():
                self._settings[vehicle_id] = AutoScheduleSettings.from_dict(settings_dict)
                self._state[vehicle_id] = AutoScheduleState(vehicle_id=vehicle_id)

            # Load cached SoC values
            self._cached_soc = stored_data.get("cached_vehicle_soc", {})

            # Restore last known SoC to state from cache
            # Create state entries for vehicles with cached SOC even if no settings exist
            for vehicle_id, soc_data in self._cached_soc.items():
                if vehicle_id not in self._state:
                    self._state[vehicle_id] = AutoScheduleState(vehicle_id=vehicle_id)

                self._state[vehicle_id].last_known_soc = soc_data.get("soc")
                if soc_data.get("updated"):
                    try:
                        self._state[vehicle_id].last_soc_update = datetime.fromisoformat(soc_data["updated"])
                    except (ValueError, TypeError):
                        pass

            if self._cached_soc:
                soc_summary = ', '.join(f"{v}={d.get('soc')}%" for v, d in self._cached_soc.items())
                _LOGGER.info(f"Restored cached SoC for {len(self._cached_soc)} vehicles: {soc_summary}")

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

            # Save cached SoC values
            stored_data["cached_vehicle_soc"] = self._cached_soc

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
            if key == "departure_times" and isinstance(value, dict):
                # Convert string keys from JSON to int day indices
                value = {int(k): v for k, v in value.items()}
            if key == "departure_priorities" and isinstance(value, dict):
                # Convert string keys from JSON to int day indices
                value = {int(k): v for k, v in value.items()}
            if key == "departure_limit_grid_import" and isinstance(value, dict):
                value = {int(k): bool(v) for k, v in value.items()}
            if key == "departure_min_battery_to_start" and isinstance(value, dict):
                value = {int(k): int(v) for k, v in value.items()}
            if key == "departure_consume_battery_level" and isinstance(value, dict):
                value = {int(k): int(v) for k, v in value.items()}
            if key == "departure_stop_at_battery_floor" and isinstance(value, dict):
                value = {int(k): bool(v) for k, v in value.items()}
            # Backward compat: map old field names to new ones
            if key == "home_battery_minimum":
                key = "min_battery_to_start"
            if key == "no_grid_import":
                key = "limit_grid_import"
            if key == "departure_no_grid_import" and isinstance(value, dict):
                key = "departure_limit_grid_import"
                value = {int(k): bool(v) for k, v in value.items()}
            if key == "departure_home_battery_min" and isinstance(value, dict):
                key = "departure_min_battery_to_start"
                value = {int(k): int(v) for k, v in value.items()}
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

    async def _cache_vehicle_soc(self, vehicle_id: str, soc: int) -> None:
        """Cache the vehicle SoC for use when vehicle is asleep.

        Saves immediately to ensure persistence across restarts.
        """
        now = datetime.now()

        # Check if SOC actually changed to avoid unnecessary saves
        old_soc = self._cached_soc.get(vehicle_id, {}).get("soc")
        soc_changed = old_soc != soc

        self._cached_soc[vehicle_id] = {
            "soc": soc,
            "updated": now.isoformat(),
        }

        # Also update state if it exists
        if vehicle_id in self._state:
            self._state[vehicle_id].last_known_soc = soc
            self._state[vehicle_id].last_soc_update = now

        _LOGGER.debug(f"Cached SoC for vehicle {vehicle_id}: {soc}%")

        # Save immediately if SOC changed (ensures persistence across restarts)
        if soc_changed and self._store is not None:
            await self._force_save_cached_soc()

    def _get_cached_soc(self, vehicle_id: str) -> Optional[int]:
        """Get cached SoC for a vehicle, or None if not cached or stale."""
        # Check state first (in-memory)
        if vehicle_id in self._state and self._state[vehicle_id].last_known_soc is not None:
            return self._state[vehicle_id].last_known_soc

        # Check persisted cache
        if vehicle_id in self._cached_soc:
            return self._cached_soc[vehicle_id].get("soc")

        # Check for _default vehicle
        if "_default" in self._cached_soc:
            return self._cached_soc["_default"].get("soc")

        return None

    async def _force_save_cached_soc(self) -> None:
        """Force save cached SoC to storage immediately."""
        if self._store is None:
            return

        try:
            stored_data = await self._store.async_load() if hasattr(self._store, 'async_load') else {}
            if not stored_data:
                stored_data = {}

            stored_data["cached_vehicle_soc"] = self._cached_soc

            if hasattr(self._store, 'async_save'):
                self._store._data = stored_data
                await self._store.async_save(stored_data)

            self._last_cache_save = datetime.now()
            _LOGGER.debug(f"Force-saved cached SoC for {len(self._cached_soc)} vehicles")
        except Exception as e:
            _LOGGER.warning(f"Failed to force-save cached SoC: {e}")

    async def _save_cached_soc_if_needed(self) -> None:
        """Save cached SoC to storage if enough time has passed since last save."""
        if self._store is None:
            return

        now = datetime.now()
        if self._last_cache_save and (now - self._last_cache_save) < self._cache_save_interval:
            return  # Too soon since last save

        await self._force_save_cached_soc()

    async def _get_vehicle_soc(self, vehicle_id: str) -> int:
        """Get current SoC for a vehicle from Home Assistant entities.

        Uses the same approach as EVVehiclesView to find Tesla vehicles.
        Caches the SoC so we can use it when the vehicle is asleep.

        Args:
            vehicle_id: Vehicle identifier

        Returns:
            Current battery level (0-100). Uses cached value if vehicle is asleep,
            or defaults to 50 if no cached value exists.
        """
        from ..const import (
            DOMAIN,
            CONF_TESLA_BLE_ENTITY_PREFIX,
            DEFAULT_TESLA_BLE_ENTITY_PREFIX,
            TESLA_BLE_SENSOR_CHARGE_LEVEL,
        )
        from homeassistant.helpers import entity_registry as er, device_registry as dr

        live_soc = None

        # Method 1: Check Tesla BLE sensor with configured prefix
        config = {}
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if entries:
            config = dict(entries[0].options)

        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
        ble_charge_level_entity = TESLA_BLE_SENSOR_CHARGE_LEVEL.format(prefix=ble_prefix)
        ble_state = self.hass.states.get(ble_charge_level_entity)

        if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
            try:
                level = float(ble_state.state)
                if 0 <= level <= 100:
                    live_soc = int(level)
                    _LOGGER.debug(f"Found Tesla BLE SoC from {ble_charge_level_entity}: {live_soc}%")
            except (ValueError, TypeError):
                pass

        # Method 2: Check Tesla Fleet/Teslemetry entities via device registry
        if live_soc is None:
            entity_registry = er.async_get(self.hass)
            device_registry = dr.async_get(self.hass)

            tesla_integrations = TESLA_INTEGRATIONS

            for device in device_registry.devices.values():
                if live_soc is not None:
                    break

                is_tesla_device = False
                for identifier in device.identifiers:
                    if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                        is_tesla_device = True
                        break

                if not is_tesla_device:
                    continue

                # Find battery/charge_level sensor for this Tesla device
                for entity in entity_registry.entities.values():
                    if entity.device_id != device.id:
                        continue

                    entity_id = entity.entity_id
                    entity_id_lower = entity_id.lower()

                    # Match battery level sensors (not power sensors, not powerwall)
                    if entity_id.startswith("sensor."):
                        # Skip powerwall entities entirely
                        if "powerwall" in entity_id_lower:
                            continue

                        # Skip power sensors (battery_power, etc)
                        if "battery_power" in entity_id_lower or entity_id_lower.endswith("_power"):
                            continue

                        # Only match explicit level sensors (battery_level, charge_level)
                        if any(x in entity_id_lower for x in ["battery_level", "charge_level", "_level"]):
                            state = self.hass.states.get(entity_id)
                            if state and state.state not in ("unavailable", "unknown", "None", None):
                                try:
                                    level = float(state.state)
                                    if 0 <= level <= 100:
                                        live_soc = int(level)
                                        _LOGGER.debug(f"Found Tesla Fleet/Teslemetry SoC from {entity_id}: {live_soc}%")
                                        break
                                except (ValueError, TypeError):
                                    continue

        # Method 3: Check cached Tesla vehicles from PowerSync
        if live_soc is None:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
            tesla_vehicles = entry_data.get("tesla_vehicles", [])
            for vehicle in tesla_vehicles:
                vid = str(vehicle.get("id", ""))
                if vehicle_id == "_default" or vehicle_id == vid or vehicle_id in vid:
                    battery_level = vehicle.get("battery_level")
                    if battery_level is not None:
                        live_soc = int(battery_level)
                        _LOGGER.debug(f"Found vehicle SoC from cached data: {live_soc}%")
                        break

        # If we got a live SoC, cache it and return
        if live_soc is not None:
            await self._cache_vehicle_soc(vehicle_id, live_soc)
            return live_soc

        # Vehicle is likely asleep - use cached SoC
        cached_soc = self._get_cached_soc(vehicle_id)
        if cached_soc is not None:
            _LOGGER.info(f"Vehicle {vehicle_id} appears asleep, using cached SoC: {cached_soc}%")
            return cached_soc

        # No cached value available - use default
        _LOGGER.warning(f"Could not find SoC for vehicle {vehicle_id} and no cached value, using default 50%")
        return 50

    async def _get_vehicle_location(self, vehicle_id: str) -> str:
        """Get current location for a vehicle from Home Assistant entities.

        Args:
            vehicle_id: Vehicle identifier

        Returns:
            Location string: "home", "work", "not_home", or "unknown"
        """
        from ..const import (
            DOMAIN,
            CONF_TESLA_BLE_ENTITY_PREFIX,
            DEFAULT_TESLA_BLE_ENTITY_PREFIX,
        )
        from homeassistant.helpers import entity_registry as er, device_registry as dr

        location = "unknown"

        # Method 1: Check Tesla Fleet/Teslemetry device_tracker entities
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        tesla_integrations = TESLA_INTEGRATIONS

        for device in device_registry.devices.values():
            if location != "unknown":
                break

            is_tesla_device = False
            device_name = None
            for identifier in device.identifiers:
                if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                    # Check if this is a vehicle (VIN format: 17 chars, not all digits)
                    id_str = str(identifier[1])
                    if len(id_str) == 17 and not id_str.isdigit():
                        is_tesla_device = True
                        device_name = device.name
                        break

            if not is_tesla_device:
                continue

            # Find location entities for this Tesla vehicle
            for entity in entity_registry.entities.values():
                if entity.device_id != device.id:
                    continue

                entity_id = entity.entity_id
                entity_id_lower = entity_id.lower()

                # Check device_tracker for location (Tesla Fleet/Teslemetry)
                if entity_id.startswith("device_tracker.") and "_location" in entity_id_lower:
                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unavailable", "unknown", "None", None):
                        location = state.state.lower()
                        _LOGGER.debug(f"Found vehicle location from {entity_id}: {location}")
                        break

                # Check binary_sensor for located_at_home (Teslemetry)
                elif entity_id.startswith("binary_sensor.") and "located_at_home" in entity_id_lower:
                    state = self.hass.states.get(entity_id)
                    if state and state.state == "on":
                        location = "home"
                        _LOGGER.debug(f"Found vehicle at home from {entity_id}")
                        break

                # Check binary_sensor for located_at_work (Teslemetry)
                elif entity_id.startswith("binary_sensor.") and "located_at_work" in entity_id_lower:
                    state = self.hass.states.get(entity_id)
                    if state and state.state == "on" and location != "home":
                        location = "work"
                        _LOGGER.debug(f"Found vehicle at work from {entity_id}")
                        break

        # Method 2 (fallback): Tesla BLE - if available, vehicle is nearby (assume "home")
        # Only used if no authoritative location found above (e.g. BLE-only users without Teslemetry)
        if location == "unknown":
            config = {}
            entries = self.hass.config_entries.async_entries(DOMAIN)
            if entries:
                config = dict(entries[0].options)

            ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
            ble_charger_entity = f"switch.{ble_prefix}_charger"
            ble_state = self.hass.states.get(ble_charger_entity)

            if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
                location = "home"
                _LOGGER.debug(f"Tesla BLE detected (fallback), assuming location=home")

        return location

    async def _is_vehicle_plugged_in(self, vehicle_id: str) -> bool:
        """Check if vehicle is plugged in from Home Assistant entities.

        Args:
            vehicle_id: Vehicle identifier

        Returns:
            True if plugged in, False otherwise
        """
        from ..const import (
            DOMAIN,
            CONF_TESLA_BLE_ENTITY_PREFIX,
            DEFAULT_TESLA_BLE_ENTITY_PREFIX,
        )
        from homeassistant.helpers import entity_registry as er, device_registry as dr

        # Method 1: Check Tesla Fleet/Teslemetry entities
        entity_registry = er.async_get(self.hass)
        device_registry = dr.async_get(self.hass)

        tesla_integrations = TESLA_INTEGRATIONS

        for device in device_registry.devices.values():
            is_tesla_device = False
            for identifier in device.identifiers:
                if len(identifier) >= 2 and identifier[0] in tesla_integrations:
                    id_str = str(identifier[1])
                    if len(id_str) == 17 and not id_str.isdigit():
                        is_tesla_device = True
                        break

            if not is_tesla_device:
                continue

            # Find plugged in sensor for this Tesla vehicle
            for entity in entity_registry.entities.values():
                if entity.device_id != device.id:
                    continue

                entity_id = entity.entity_id
                entity_id_lower = entity_id.lower()

                # Check binary_sensor for charge_cable (plugged in)
                if entity_id.startswith("binary_sensor.") and "charge_cable" in entity_id_lower:
                    state = self.hass.states.get(entity_id)
                    if state:
                        if state.state in ("unavailable", "unknown"):
                            # Car likely asleep — check location to determine if still plugged in
                            location = await self._get_vehicle_location(vehicle_id)
                            if location == "home":
                                _LOGGER.debug(f"Charge cable {entity_id} is {state.state} but car is home, treating as plugged in")
                                return True
                            else:
                                _LOGGER.debug(f"Charge cable {entity_id} is {state.state} and car not home, treating as unplugged")
                                return False
                        is_plugged = state.state == "on"
                        _LOGGER.debug(f"Found plugged in state from {entity_id}: {is_plugged}")
                        return is_plugged

                # Also check charging state sensor
                elif entity_id.startswith("sensor.") and "_charging" in entity_id_lower and "charging_" not in entity_id_lower:
                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unavailable", "unknown", "None", None):
                        # If actively charging, must be plugged in
                        if state.state.lower() in ("charging", "complete", "stopped"):
                            _LOGGER.debug(f"Vehicle plugged in (charging state: {state.state})")
                            return True

        # Method 2 (fallback): Tesla BLE — only if no authoritative sensor found above
        config = {}
        entries = self.hass.config_entries.async_entries(DOMAIN)
        if entries:
            config = dict(entries[0].options)

        ble_prefix = config.get(CONF_TESLA_BLE_ENTITY_PREFIX, DEFAULT_TESLA_BLE_ENTITY_PREFIX)
        ble_charger_entity = f"switch.{ble_prefix}_charger"
        ble_state = self.hass.states.get(ble_charger_entity)

        if ble_state and ble_state.state not in ("unavailable", "unknown", "None", None):
            _LOGGER.debug(f"Tesla BLE detected (fallback), assuming plugged in")
            return True

        return False

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
                # Restore curtailment if modified and auto-schedule is now disabled
                state = self._state.get(vehicle_id)
                if state:
                    if state.curtailment_override_active:
                        _LOGGER.info(
                            f"Auto-schedule disabled for {vehicle_id}, restoring curtailment"
                        )
                        await self._restore_curtailment(state)
                    # Also stop charging if still active
                    if state.is_charging:
                        await self._stop_charging(vehicle_id, settings, state)
                continue

            try:
                await self._evaluate_vehicle(vehicle_id, settings, live_status, current_price_cents)
            except Exception as e:
                _LOGGER.error(f"Auto-schedule evaluation failed for {vehicle_id}: {e}")

        # Periodically save cached SoC values to storage
        await self._save_cached_soc_if_needed()

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

        # Check if vehicle is at home - only charge at home
        location = await get_ev_location(self.hass, self.config_entry)
        if location not in ("home", "unknown"):
            # Vehicle is away - don't try to charge
            if state.is_charging:
                # Stop any active charging session tracking
                state.is_charging = False
            state.last_decision = "away"
            state.last_decision_reason = f"Vehicle not at home (location: {location})"
            _LOGGER.debug(f"Auto-schedule: Vehicle {vehicle_id} not at home ({location}), skipping")
            return

        # Check if vehicle is plugged in
        plugged_in = await is_ev_plugged_in(self.hass, self.config_entry)
        if not plugged_in:
            if state.is_charging:
                state.is_charging = False
            state.last_decision = "unplugged"
            state.last_decision_reason = "Vehicle not plugged in"
            _LOGGER.debug(f"Auto-schedule: Vehicle {vehicle_id} not plugged in, skipping")
            return

        # Get EV's current SoC to check if we've reached target
        ev_soc = await self._get_vehicle_soc(vehicle_id)

        # Check if EV has reached target SoC
        if ev_soc >= settings.target_soc:
            # Stop charging if still charging
            if state.is_charging:
                await self._stop_charging(vehicle_id, settings, state)
                state.last_decision = "complete"
                state.last_decision_reason = f"EV reached target {settings.target_soc}%"
                return
            else:
                state.last_decision = "complete"
                state.last_decision_reason = f"EV at {ev_soc}% (target: {settings.target_soc}%)"
                return

        # =====================================================================
        # SMART OPTIMIZATION INTEGRATION
        # When Smart Optimization is enabled, use its schedule instead of the
        # built-in charging planner. The optimizer considers home battery,
        # solar, prices, and EV charging jointly for whole-home optimization.
        #
        # VARIABLE CHARGE RATE: The optimizer outputs target power (kW) per
        # interval. We convert this to amps and set the charge rate dynamically
        # to match solar surplus, minimize costs, or maximize self-consumption.
        # =====================================================================
        ml_schedule = self._get_ml_ev_schedule(vehicle_id)
        if ml_schedule is not None:
            should_charge, power_w = ml_schedule.should_charge_at(now)

            # Get next charging window for status display
            next_start, next_end, next_power = ml_schedule.get_next_charging_window(now)

            # Calculate target amps for logging
            target_amps = self._power_to_amps(power_w) if power_w > 0 else 0

            if should_charge:
                reason = f"Smart Optimization: charge at {power_w/1000:.1f}kW ({target_amps}A)"
                source = "ml_optimized"

                if not state.is_charging:
                    # Start charging
                    await self._start_charging(vehicle_id, settings, state, source)
                    state.last_decision = "started"
                    state.last_decision_reason = reason
                    _LOGGER.info(f"🤖 ML EV Charging: Starting charge for {vehicle_id} at {power_w/1000:.1f}kW ({target_amps}A)")

                # Set variable charge rate (whether just started or already charging)
                # This allows ramping the charge rate based on solar/prices
                await self._set_vehicle_charge_rate(vehicle_id, power_w, settings)
                state.last_decision = "charging"
                state.last_decision_reason = reason

            else:
                if next_start:
                    reason = f"Smart Optimization: next window {next_start.strftime('%H:%M')} - {next_end.strftime('%H:%M')}"
                else:
                    reason = "Smart Optimization: no charging scheduled"

                if state.is_charging:
                    await self._stop_charging(vehicle_id, settings, state)
                    state.last_decision = "stopped"
                    state.last_decision_reason = reason
                    # Clear tracked charge rate
                    self._current_charge_amps.pop(vehicle_id, None)
                    _LOGGER.info(f"🤖 ML EV Charging: Stopping charge for {vehicle_id} - {reason}")
                else:
                    state.last_decision = "waiting"
                    state.last_decision_reason = reason

            # Skip the normal planning logic when using Smart Optimization
            return

        # =====================================================================
        # STANDARD CHARGING PLANNER (when Smart Optimization not available)
        # =====================================================================

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

        # Note: min_battery_soc affects surplus calculation (prevents discharge),
        # but does NOT block EV charging from solar or grid.
        # The Powerwall's own backup reserve handles discharge protection.
        weekday = datetime.now().weekday()
        effective_priority = settings.get_effective_priority(weekday)
        effective_limit_grid = settings.get_effective_limit_grid_import(weekday)
        effective_max_price = settings.get_effective_max_grid_price(weekday)
        effective_home_min = settings.get_effective_min_battery_to_start(weekday)
        effective_consume_level = settings.get_effective_consume_battery_level(weekday)
        effective_stop_at_floor = settings.get_effective_stop_at_battery_floor(weekday)
        is_time_critical = effective_priority == ChargingPriority.TIME_CRITICAL

        # Consume battery logic: if consume_battery_level > 0, allow charging while
        # battery is above the consume level. When battery hits the floor:
        # - stop_at_battery_floor=True: block charging entirely
        # - stop_at_battery_floor=False: allow grid charging (planner proceeds normally)
        if effective_consume_level > 0 and battery_soc <= effective_consume_level:
            if effective_stop_at_floor:
                reason = (
                    f"Battery {battery_soc:.0f}% at consume floor {effective_consume_level}% — "
                    f"EV charging stopped (stop at floor enabled)"
                )
                if state.is_charging:
                    await self._stop_charging(vehicle_id, settings, state)
                state.last_decision = "waiting"
                state.last_decision_reason = reason
                return

        # Use planner's should_charge_now logic
        should_charge, reason, source = await self.planner.should_charge_now(
            vehicle_id=vehicle_id,
            plan=state.current_plan,
            current_surplus_kw=current_surplus_kw,
            current_price_cents=current_price_cents,
            battery_soc=battery_soc,
            min_battery_soc=effective_home_min,
            is_time_critical=is_time_critical,
        )

        # Apply additional constraints based on priority mode
        if should_charge and source.startswith("grid"):
            # Check price constraint (but not for time_critical - deadline takes priority)
            if current_price_cents > effective_max_price and not is_time_critical:
                should_charge = False
                reason = f"Grid price {current_price_cents:.0f}c > max {effective_max_price:.0f}c"

            # Solar-only mode doesn't allow grid
            elif effective_priority == ChargingPriority.SOLAR_ONLY:
                should_charge = False
                reason = "Solar-only mode - no grid charging"

        # Check surplus constraint for solar charging
        # Tesla requires minimum 5A to charge:
        # - Single phase: 5A × 230V = 1.15kW
        # - Three phase: 5A × 230V × 3 = 3.45kW
        if should_charge and source == "solar_surplus":
            # Get solar surplus config to check home_battery_minimum and parallel charging settings
            solar_config = await self._get_solar_surplus_config()
            min_battery_for_ev = solar_config.get("home_battery_minimum", 80)
            allow_parallel = solar_config.get("allow_parallel_charging", False)
            max_battery_charge_kw = solar_config.get("max_battery_charge_rate_kw", 5.0)

            # Get home power settings for phases
            home_power = await self._get_home_power_settings()
            phases = 3 if home_power.get("phase_type") == "three" else 1

            # Check if battery needs priority (battery below threshold)
            if battery_soc < min_battery_for_ev:
                # Check if parallel charging is available
                parallel_available = allow_parallel and current_surplus_kw > max_battery_charge_kw

                if parallel_available:
                    # Parallel charging: surplus exceeds battery's max charge rate
                    # Calculate available surplus for EV (after reserving max for battery)
                    ev_surplus_kw = current_surplus_kw - max_battery_charge_kw
                    min_charge_amps = 5  # Tesla minimum
                    voltage = 230  # Australia standard
                    min_surplus = (min_charge_amps * voltage * phases) / 1000

                    if ev_surplus_kw >= min_surplus:
                        reason = (
                            f"Parallel charging: surplus {current_surplus_kw:.1f}kW > "
                            f"battery max {max_battery_charge_kw}kW, EV gets {ev_surplus_kw:.1f}kW"
                        )
                        _LOGGER.info(
                            f"Auto-schedule: Parallel charging enabled - battery at {battery_soc:.0f}%, "
                            f"surplus {current_surplus_kw:.1f}kW > battery max {max_battery_charge_kw}kW"
                        )
                    else:
                        should_charge = False
                        reason = (
                            f"Parallel surplus {ev_surplus_kw:.1f}kW < min {min_surplus:.1f}kW "
                            f"(total {current_surplus_kw:.1f}kW - battery {max_battery_charge_kw}kW)"
                        )
                else:
                    should_charge = False
                    if allow_parallel:
                        reason = (
                            f"Battery {battery_soc:.0f}% < {min_battery_for_ev}%, "
                            f"surplus {current_surplus_kw:.1f}kW <= battery max {max_battery_charge_kw}kW"
                        )
                    else:
                        reason = f"Battery {battery_soc:.0f}% < {min_battery_for_ev}% (charging battery first)"
                    _LOGGER.info(
                        f"Auto-schedule: Solar surplus blocked - battery at {battery_soc:.0f}% "
                        f"needs to reach {min_battery_for_ev}% before EV charging"
                    )
            else:
                # Battery is above threshold, check surplus requirement
                # Calculate min surplus from home power settings
                min_charge_amps = 5  # Tesla minimum
                voltage = 230  # Australia standard
                min_surplus = (min_charge_amps * voltage * phases) / 1000
                if current_surplus_kw < min_surplus:
                    should_charge = False
                    reason = f"Surplus {current_surplus_kw:.1f}kW < min {min_surplus:.1f}kW"
                    _LOGGER.info(
                        f"Auto-schedule: In solar window but no surplus - "
                        f"solar={solar_power_kw:.1f}kW, load={load_power_kw:.1f}kW, "
                        f"surplus={current_surplus_kw:.1f}kW < {min_surplus:.1f}kW needed (phases={phases})"
                    )

        # Find current window (if in one)
        current_window = None
        for window in state.current_plan.windows:
            window_start = datetime.fromisoformat(window.start_time)
            window_end = datetime.fromisoformat(window.end_time)
            if window_start <= now < window_end:
                current_window = window
                break

        state.current_window = current_window

        # Log the decision
        _LOGGER.debug(
            f"Auto-schedule decision for {vehicle_id}: should_charge={should_charge}, "
            f"reason={reason}, source={source}, is_charging={state.is_charging}"
        )

        # Take action
        if should_charge and not state.is_charging:
            await self._start_charging(vehicle_id, settings, state, source)
            state.last_decision = "started"
            state.last_decision_reason = reason
        elif not should_charge and state.is_charging:
            # Restore backup reserve when stopping - we'll set it again when next window starts
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

        # Determine target time from per-day departure_times
        target_time = None
        if settings.departure_times:
            # Find next applicable departure by walking forward through days
            for days_ahead in range(8):  # Check up to 7 days ahead
                check_time = now + timedelta(days=days_ahead)
                weekday = check_time.weekday()
                if weekday in settings.departure_times:
                    dep_str = settings.departure_times[weekday]
                    try:
                        dep_hour, dep_min = map(int, dep_str.split(":"))
                        candidate = check_time.replace(hour=dep_hour, minute=dep_min, second=0, microsecond=0)
                        if candidate > now:
                            target_time = candidate
                            break
                    except ValueError:
                        _LOGGER.warning(f"Invalid departure time format for day {weekday}: {dep_str}")
        elif settings.departure_time:
            # Legacy fallback: single departure_time + departure_days
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

        # Get current SoC from vehicle sensors
        current_soc = await self._get_vehicle_soc(vehicle_id)

        try:
            # Use per-day priority based on the target departure day
            effective_priority = settings.get_effective_priority(
                target_time.weekday() if target_time else now.weekday()
            )
            plan = await self.planner.plan_charging(
                vehicle_id=vehicle_id,
                current_soc=current_soc,
                target_soc=settings.target_soc,
                target_time=target_time,
                priority=effective_priority,
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
        """Get current import price from available sources (provider-aware).

        Uses real-time TOU calculation for custom/Tesla tariffs to ensure prices
        update when TOU periods change throughout the day.
        """
        from ..const import DOMAIN, CONF_ELECTRICITY_PROVIDER
        from ..__init__ import get_current_price_from_tariff_schedule

        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})

            # Get electricity provider
            electricity_provider = self.config_entry.options.get(
                CONF_ELECTRICITY_PROVIDER,
                self.config_entry.data.get(CONF_ELECTRICITY_PROVIDER, "amber")
            )

            if electricity_provider in ("amber", "flow_power"):
                # Amber/Flow Power: Read from coordinator data (live API prices)
                amber_coordinator = entry_data.get("amber_coordinator")
                if amber_coordinator and amber_coordinator.data:
                    current_prices = amber_coordinator.data.get("current", [])
                    for price in current_prices:
                        if price.get("channelType") == "general":
                            # perKwh is in cents for Amber
                            return price.get("perKwh", 30.0)

            elif electricity_provider in ("globird", "aemo_vpp"):
                # Globird/AEMO VPP: Use real-time TOU calculation from tariff schedule
                tariff_schedule = entry_data.get("tariff_schedule", {})
                if tariff_schedule:
                    # Use real-time TOU calculation if TOU periods are defined
                    if tariff_schedule.get("tou_periods"):
                        buy_cents, _, current_period = get_current_price_from_tariff_schedule(tariff_schedule)
                        _LOGGER.debug(f"Current price from TOU: {buy_cents}c ({current_period})")
                        return buy_cents
                    # Fallback to cached buy_price
                    buy_price = tariff_schedule.get("buy_price")
                    if buy_price is not None:
                        return buy_price  # Already in cents

            # Fallback: Try tariff schedule with TOU calculation for any provider
            tariff_schedule = entry_data.get("tariff_schedule", {})
            if tariff_schedule:
                # Real-time TOU calculation
                if tariff_schedule.get("tou_periods"):
                    buy_cents, _, _ = get_current_price_from_tariff_schedule(tariff_schedule)
                    return buy_cents

                # Try Amber format with PERIOD_HH_MM keys
                now = datetime.now()
                period_key = f"PERIOD_{now.hour:02d}_{30 if now.minute >= 30 else 0:02d}"
                buy_prices = tariff_schedule.get("buy_prices", {})
                if period_key in buy_prices:
                    return buy_prices[period_key] * 100

            # Fallback: Try Sigenergy tariff (for Sigenergy users with Amber)
            sigenergy_tariff = entry_data.get("sigenergy_tariff", {})
            if sigenergy_tariff:
                buy_prices = sigenergy_tariff.get("buy_prices", [])
                if buy_prices:
                    # Find current time slot price
                    # Format: [{"timeRange": "10:00-10:30", "price": 25.0}, ...]
                    now = datetime.now()
                    current_time = f"{now.hour:02d}:{30 if now.minute >= 30 else 0:02d}"
                    for slot in buy_prices:
                        time_range = slot.get("timeRange", "")
                        if time_range.startswith(current_time):
                            return slot.get("price", 30.0)  # Already in cents

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

    def _is_sigenergy_system(self) -> bool:
        """Check if this is a SigEnergy system (vs Tesla Powerwall)."""
        from ..const import CONF_SIGENERGY_STATION_ID
        return bool(self.config_entry.data.get(CONF_SIGENERGY_STATION_ID))

    async def _get_solar_surplus_config(self) -> dict:
        """Get the solar surplus config from storage.

        Returns:
            Config dict with min_battery_soc, household_buffer_kw, etc.
        """
        try:
            from ..const import DOMAIN
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})

            # Try to get from automation_store
            automation_store = entry_data.get("automation_store")
            if automation_store:
                stored_data = getattr(automation_store, '_data', {}) or {}
                config = stored_data.get("solar_surplus_config", {})
                if config:
                    return config

            # Return defaults
            return {
                "enabled": False,
                "household_buffer_kw": 0.5,
                "sustained_surplus_minutes": 2,
                "stop_delay_minutes": 5,
                "home_battery_minimum": 80,
            }
        except Exception as e:
            _LOGGER.debug(f"Failed to get solar surplus config: {e}")
            return {"home_battery_minimum": 80}

    async def _get_home_power_settings(self) -> dict:
        """Get home power settings from storage.

        Returns:
            Config dict with phase_type, max_amps_per_phase, etc.
        """
        try:
            from ..const import DOMAIN
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})

            # Try to get from automation_store
            automation_store = entry_data.get("automation_store")
            if automation_store:
                stored_data = getattr(automation_store, '_data', {}) or {}
                config = stored_data.get("home_power_settings", {})
                if config:
                    return config

            # Return defaults
            return {
                "phase_type": "single",
                "max_charge_speed_enabled": False,
                "max_amps_per_phase": 32,
            }
        except Exception as e:
            _LOGGER.debug(f"Failed to get home power settings: {e}")
            return {"phase_type": "single", "max_amps_per_phase": 32}

    async def _get_sigenergy_controller(self):
        """Get a SigEnergy controller instance."""
        from ..const import (
            CONF_SIGENERGY_MODBUS_HOST,
            CONF_SIGENERGY_MODBUS_PORT,
            CONF_SIGENERGY_MODBUS_SLAVE_ID,
        )
        from ..inverters.sigenergy import SigenergyController

        modbus_host = self.config_entry.options.get(
            CONF_SIGENERGY_MODBUS_HOST,
            self.config_entry.data.get(CONF_SIGENERGY_MODBUS_HOST)
        )
        if not modbus_host:
            _LOGGER.warning("SigEnergy Modbus host not configured")
            return None

        modbus_port = self.config_entry.options.get(
            CONF_SIGENERGY_MODBUS_PORT,
            self.config_entry.data.get(CONF_SIGENERGY_MODBUS_PORT, 502)
        )
        modbus_slave_id = self.config_entry.options.get(
            CONF_SIGENERGY_MODBUS_SLAVE_ID,
            self.config_entry.data.get(CONF_SIGENERGY_MODBUS_SLAVE_ID, 247)
        )

        return SigenergyController(
            host=modbus_host,
            port=modbus_port,
            slave_id=modbus_slave_id,
        )

    async def _get_current_backup_reserve(self) -> Optional[int]:
        """Get the current battery backup reserve percentage.

        Supports both Tesla Powerwall and SigEnergy systems.
        """
        # Check if SigEnergy system
        if self._is_sigenergy_system():
            return await self._get_sigenergy_backup_reserve()

        # Tesla Powerwall
        return await self._get_tesla_backup_reserve()

    async def _get_sigenergy_backup_reserve(self) -> Optional[int]:
        """Get backup reserve from SigEnergy via Modbus."""
        try:
            controller = await self._get_sigenergy_controller()
            if not controller:
                return None

            reserve = await controller.get_backup_reserve()
            await controller.disconnect()

            _LOGGER.debug(f"SigEnergy backup reserve: {reserve}%")
            return reserve

        except Exception as e:
            _LOGGER.error(f"Error getting SigEnergy backup reserve: {e}")
            return None

    async def _get_tesla_backup_reserve(self) -> Optional[int]:
        """Get backup reserve from Tesla Powerwall via Fleet API."""
        try:
            from ..const import (
                CONF_TESLA_ENERGY_SITE_ID,
                TESLA_PROVIDER_TESLEMETRY,
                TESLEMETRY_API_BASE_URL,
                FLEET_API_BASE_URL,
            )
            from .. import get_tesla_api_token

            current_token, provider = get_tesla_api_token(self.hass, self.config_entry)
            site_id = self.config_entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                _LOGGER.debug("No Tesla site ID or token for backup reserve")
                return None

            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    site_info = data.get("response", {})
                    reserve = site_info.get("backup_reserve_percent")
                    _LOGGER.debug(f"Tesla backup reserve: {reserve}%")
                    return reserve
                else:
                    _LOGGER.warning(f"Failed to get Tesla backup reserve: {response.status}")
                    return None

        except Exception as e:
            _LOGGER.error(f"Error getting Tesla backup reserve: {e}")
            return None

    async def _set_backup_reserve(self, percent: int) -> bool:
        """Set the battery backup reserve percentage.

        Supports both Tesla Powerwall and SigEnergy systems.
        """
        # Check if SigEnergy system
        if self._is_sigenergy_system():
            return await self._set_sigenergy_backup_reserve(percent)

        # Tesla Powerwall
        return await self._set_tesla_backup_reserve(percent)

    async def _set_sigenergy_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve on SigEnergy via Modbus."""
        try:
            controller = await self._get_sigenergy_controller()
            if not controller:
                return False

            success = await controller.set_backup_reserve(percent)
            await controller.disconnect()

            if success:
                _LOGGER.info(f"✅ EV Charging: Set SigEnergy backup reserve to {percent}%")
            return success

        except Exception as e:
            _LOGGER.error(f"Error setting SigEnergy backup reserve: {e}")
            return False

    async def _set_tesla_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve on Tesla Powerwall via Fleet API."""
        try:
            from ..const import (
                CONF_TESLA_ENERGY_SITE_ID,
                TESLA_PROVIDER_TESLEMETRY,
                TESLEMETRY_API_BASE_URL,
                FLEET_API_BASE_URL,
            )
            from .. import get_tesla_api_token

            current_token, provider = get_tesla_api_token(self.hass, self.config_entry)
            site_id = self.config_entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                _LOGGER.warning("No Tesla site ID or token for setting backup reserve")
                return False

            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/backup",
                headers=headers,
                json={"backup_reserve_percent": percent},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ EV Charging: Set Tesla backup reserve to {percent}%")
                    return True
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set Tesla backup reserve: {response.status} - {text}")
                    return False

        except Exception as e:
            _LOGGER.error(f"Error setting Tesla backup reserve: {e}")
            return False

    async def _get_current_export_rule(self) -> Optional[str]:
        """Get the current grid export rule.

        Returns:
            Export rule: "never", "pv_only", or "battery_ok"
        """
        # Only Tesla Powerwall supports export rule control
        if self._is_sigenergy_system():
            return None

        try:
            from ..const import (
                CONF_TESLA_ENERGY_SITE_ID,
                TESLA_PROVIDER_TESLEMETRY,
                TESLEMETRY_API_BASE_URL,
                FLEET_API_BASE_URL,
            )
            from .. import get_tesla_api_token

            current_token, provider = get_tesla_api_token(self.hass, self.config_entry)
            site_id = self.config_entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                return None

            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            async with session.get(
                f"{api_base}/api/1/energy_sites/{site_id}/site_info",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    site_info = data.get("response", {})
                    components = site_info.get("components", {})
                    # Map Tesla API values to our rules
                    disallow_export = components.get("disallow_charge_from_grid_with_solar_installed", False)
                    customer_preferred = components.get("customer_preferred_export_rule")
                    if customer_preferred:
                        return customer_preferred
                    return "never" if disallow_export else "pv_only"
                return None

        except Exception as e:
            _LOGGER.debug(f"Error getting export rule: {e}")
            return None

    async def _set_export_rule(self, rule: str) -> bool:
        """Set the grid export rule.

        Args:
            rule: "never", "pv_only", or "battery_ok"

        Returns:
            True if successful
        """
        # Only Tesla Powerwall supports export rule control
        if self._is_sigenergy_system():
            _LOGGER.debug("SigEnergy does not support export rule control")
            return False

        if rule not in ("never", "pv_only", "battery_ok"):
            _LOGGER.warning(f"Invalid export rule: {rule}")
            return False

        try:
            from ..const import (
                CONF_TESLA_ENERGY_SITE_ID,
                TESLA_PROVIDER_TESLEMETRY,
                TESLEMETRY_API_BASE_URL,
                FLEET_API_BASE_URL,
            )
            from .. import get_tesla_api_token

            current_token, provider = get_tesla_api_token(self.hass, self.config_entry)
            site_id = self.config_entry.data.get(CONF_TESLA_ENERGY_SITE_ID)

            if not site_id or not current_token:
                _LOGGER.warning("No Tesla site ID or token for setting export rule")
                return False

            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            headers = {
                "Authorization": f"Bearer {current_token}",
                "Content-Type": "application/json",
            }
            api_base = TESLEMETRY_API_BASE_URL if provider == TESLA_PROVIDER_TESLEMETRY else FLEET_API_BASE_URL

            # Map our rule names to Tesla API
            disallow_export = rule == "never"

            async with session.post(
                f"{api_base}/api/1/energy_sites/{site_id}/grid_import_export",
                headers=headers,
                json={
                    "disallow_charge_from_grid_with_solar_installed": disallow_export,
                    "customer_preferred_export_rule": rule,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status == 200:
                    _LOGGER.info(f"✅ EV Charging: Set grid export rule to '{rule}'")
                    return True
                else:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set export rule: {response.status} - {text}")
                    return False

        except Exception as e:
            _LOGGER.error(f"Error setting export rule: {e}")
            return False

    async def _disable_curtailment_for_ev(self, state: AutoScheduleState) -> bool:
        """Disable curtailment to allow full solar production for EV charging.

        When solar surplus EV charging starts, we want to use all available solar
        rather than curtailing it. This sets export rule to 'pv_only' and marks
        that we've overridden the curtailment system.

        Args:
            state: The vehicle's auto-schedule state

        Returns:
            True if curtailment was disabled (or already disabled)
        """
        if state.curtailment_override_active:
            return True  # Already overridden

        # Get current export rule
        current_rule = await self._get_current_export_rule()
        if current_rule is None:
            _LOGGER.debug("Could not get current export rule, skipping curtailment override")
            return False

        # Only override if currently curtailed (export = never)
        if current_rule != "never":
            _LOGGER.debug(f"Export rule is '{current_rule}', no curtailment override needed")
            return True

        # Save original rule and set to pv_only to allow full solar production
        state.original_export_rule = current_rule
        if await self._set_export_rule("pv_only"):
            state.curtailment_override_active = True
            _LOGGER.info(
                f"☀️ EV Charging: Disabled curtailment for solar surplus charging "
                f"(export rule: never → pv_only)"
            )

            # Mark this as EV override so curtailment scheduler doesn't immediately revert it
            from ..const import DOMAIN
            entry_data = self.hass.data.setdefault(DOMAIN, {}).setdefault(self.config_entry.entry_id, {})
            entry_data["ev_curtailment_override"] = True
            entry_data["cached_export_rule"] = "pv_only"

            return True
        return False

    async def _restore_curtailment(self, state: AutoScheduleState) -> None:
        """Restore curtailment after EV charging stops.

        Args:
            state: The vehicle's auto-schedule state
        """
        if not state.curtailment_override_active:
            return

        # Clear the EV override flag first
        from ..const import DOMAIN
        entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id, {})
        if entry_data:
            entry_data.pop("ev_curtailment_override", None)

        # Restore original export rule if we saved one
        if state.original_export_rule:
            if await self._set_export_rule(state.original_export_rule):
                _LOGGER.info(
                    f"☀️ EV Charging: Restored curtailment after charging stopped "
                    f"(export rule: pv_only → {state.original_export_rule})"
                )
                # Update cached rule
                if entry_data:
                    entry_data["cached_export_rule"] = state.original_export_rule
            else:
                _LOGGER.warning("Failed to restore export rule after EV charging")

        state.curtailment_override_active = False
        state.original_export_rule = None

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
            # Disable curtailment to allow full solar production for EV charging
            # This prevents solar being curtailed when we could use it to charge the EV
            await self._disable_curtailment_for_ev(state)
        else:
            dynamic_mode = "battery_target"

        # Only pass vehicle_vin if it looks like a valid VIN (17 chars)
        # Otherwise use None to let the system find the default vehicle
        vehicle_vin = None
        if vehicle_id and vehicle_id != "_default" and len(vehicle_id) == 17:
            vehicle_vin = vehicle_id

        params = {
            "vehicle_vin": vehicle_vin,
            "vehicle_name": settings.display_name,
            "dynamic_mode": dynamic_mode,
            "min_charge_amps": settings.min_charge_amps,
            "max_charge_amps": settings.max_charge_amps,
            "voltage": settings.voltage,
            "charger_type": settings.charger_type,
            "min_battery_soc": settings.get_effective_min_battery_to_start(datetime.now().weekday()),
            "pause_below_soc": (
                settings.get_effective_consume_battery_level(datetime.now().weekday())
                if settings.get_effective_consume_battery_level(datetime.now().weekday()) > 0
                else max(0, settings.get_effective_min_battery_to_start(datetime.now().weekday()) - 10)
            ),
            "stop_at_battery_floor": settings.get_effective_stop_at_battery_floor(datetime.now().weekday()),
            "charger_switch_entity": settings.charger_switch_entity,
            "charger_amps_entity": settings.charger_amps_entity,
            "ocpp_charger_id": settings.ocpp_charger_id,
            "no_grid_import": settings.get_effective_limit_grid_import(datetime.now().weekday()),
        }

        try:
            success = await _action_start_ev_charging_dynamic(
                self.hass, self.config_entry, params, context=None
            )

            if success:
                state.is_charging = True
                state.started_at = datetime.now()
                _LOGGER.info(f"Auto-schedule: Started {dynamic_mode} charging for {vehicle_id}")
                # Note: Notifications are sent by _action_start_ev_charging_dynamic
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

        # Only pass vehicle_id if it looks like a valid VIN (17 chars)
        vid = None
        if vehicle_id and vehicle_id != "_default" and len(vehicle_id) == 17:
            vid = vehicle_id

        params = {"vehicle_vin": vid}

        try:
            await _action_stop_ev_charging_dynamic(self.hass, self.config_entry, params)
            state.is_charging = False
            state.started_at = None
            state.current_window = None
            _LOGGER.info(f"Auto-schedule: Stopped charging for {vehicle_id}")
            # Note: Notifications are sent by _action_stop_ev_charging_dynamic

            # Always restore curtailment when stopping (if it was overridden)
            await self._restore_curtailment(state)

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


# ============================================================================
# PRICE-LEVEL CHARGING EXECUTOR
# ============================================================================

@dataclass
class PriceLevelChargingState:
    """State for price-level charging."""
    is_charging: bool = False
    last_decision: str = "idle"
    last_decision_reason: str = ""
    charging_mode: str = ""  # "recovery" or "opportunity"


class PriceLevelChargingExecutor:
    """
    Executes price-level charging based on current price thresholds.

    Two modes:
    - Recovery: Below recovery_soc, charge when price <= recovery_price_cents
    - Opportunity: Above recovery_soc, charge when price <= opportunity_price_cents
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
    ):
        from ..const import DOMAIN
        self.hass = hass
        self.config_entry = config_entry
        self._domain = DOMAIN
        self._state = PriceLevelChargingState()  # Legacy single-vehicle state
        self._vehicle_states: Dict[str, PriceLevelChargingState] = {}  # Per-VIN state tracking

    def _get_settings(self) -> dict:
        """Get price-level charging settings from store."""
        entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
        store = entry_data.get("automation_store")

        defaults = {
            "enabled": False,
            "recovery_soc": 40,
            "recovery_price_cents": 30,
            "opportunity_price_cents": 10,
            "no_grid_import": False,
            "min_battery_to_start": 20,  # Don't charge EV if home battery below this %
            "home_battery_minimum": 20,  # Backward compat alias
        }

        if store:
            stored_data = getattr(store, '_data', {}) or {}
            settings = stored_data.get("price_level_charging", {})
            _LOGGER.debug(
                f"Price-level settings from store: {settings}, "
                f"store._data keys: {list(stored_data.keys())}"
            )
            defaults.update(settings)
        else:
            _LOGGER.warning("Price-level charging: automation_store not found in entry_data")

        return defaults

    async def _get_home_battery_soc(self) -> Optional[float]:
        """Get home battery (Powerwall/Sigenergy/Sungrow) state of charge.

        Returns the battery percentage from the Tesla coordinator or other sources.
        """
        # Try to get from Tesla coordinator
        entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
        tesla_coordinator = entry_data.get("tesla_coordinator")

        if tesla_coordinator and tesla_coordinator.data:
            battery_level = tesla_coordinator.data.get("battery_level")
            if battery_level is not None:
                return float(battery_level)

        # Fallback: Try to find battery level from common entity patterns
        entity_patterns = [
            "sensor.powerwall_charge",
            "sensor.powerwall_battery_remaining",
            "sensor.sigenergy_battery_soc",
            "sensor.sungrow_battery_soc",
        ]

        for pattern in entity_patterns:
            # Check for entities matching the pattern
            for entity_id in self.hass.states.async_entity_ids("sensor"):
                if pattern.replace("*", "") in entity_id.lower() or entity_id == pattern:
                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unknown", "unavailable"):
                        try:
                            return float(state.state)
                        except (ValueError, TypeError):
                            continue

        return None

    async def _get_ev_soc(self, vehicle_vin: Optional[str] = None) -> Optional[int]:
        """Get EV's current state of charge from HA entities.

        Searches for battery level sensors from various Tesla integrations:
        - Teslemetry (sensor.*_battery_level)
        - Tesla Custom Integration
        - Tesla BLE

        Args:
            vehicle_vin: Optional VIN to check specific vehicle. If None, returns
                         SoC of first vehicle found (backward compatible).
        """
        from homeassistant.helpers import entity_registry as er, device_registry as dr

        # Method 1: Check tesla_vehicles in entry_data (legacy)
        entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
        tesla_vehicles = entry_data.get("tesla_vehicles", [])

        for vehicle in tesla_vehicles:
            # If VIN specified, only return matching vehicle's battery level
            if vehicle_vin is not None:
                if vehicle.get("vin") == vehicle_vin:
                    battery_level = vehicle.get("battery_level")
                    if battery_level is not None:
                        return int(battery_level)
            else:
                battery_level = vehicle.get("battery_level")
                if battery_level is not None:
                    return int(battery_level)

        # Method 2: Search HA entity registry for EV battery sensors
        try:
            entity_reg = er.async_get(self.hass)
            device_reg = dr.async_get(self.hass)

            # Find Tesla devices (with VIN mapping)
            tesla_device_map: Dict[str, str] = {}  # device_id -> VIN
            for device in device_reg.devices.values():
                # Check various Tesla integration identifiers
                for identifier in device.identifiers:
                    if len(identifier) >= 2:
                        domain = identifier[0]
                        id_str = str(identifier[1])
                        if domain in TESLA_INTEGRATIONS or domain in ("tesla_ble", "tesla_bluetooth"):
                            # Check if identifier is a VIN (17 chars, not all digits)
                            if len(id_str) == 17 and not id_str.isdigit():
                                tesla_device_map[device.id] = id_str
                            else:
                                # Non-VIN identifier, use device.id as fallback
                                if device.id not in tesla_device_map:
                                    tesla_device_map[device.id] = ""
                            break

            # Search for battery level sensors
            for entity in entity_reg.entities.values():
                if entity.device_id not in tesla_device_map:
                    continue

                # If specific VIN requested, skip other vehicles
                device_vin = tesla_device_map.get(entity.device_id, "")
                if vehicle_vin is not None and device_vin and device_vin != vehicle_vin:
                    continue

                entity_id = entity.entity_id
                entity_id_lower = entity_id.lower()

                # Match battery level sensors
                if entity_id.startswith("sensor.") and any(
                    x in entity_id_lower for x in ["battery_level", "charge_level", "battery"]
                ):
                    # Skip power sensors
                    if "power" in entity_id_lower or "range" in entity_id_lower:
                        continue

                    state = self.hass.states.get(entity_id)
                    if state and state.state not in ("unavailable", "unknown", "None", None):
                        try:
                            level = float(state.state)
                            if 0 <= level <= 100:
                                _LOGGER.debug(f"Found EV battery level from {entity_id} (VIN: {device_vin}): {level}%")
                                return int(level)
                        except (ValueError, TypeError):
                            continue

        except Exception as e:
            _LOGGER.debug(f"Error searching for EV battery sensor: {e}")

        # Method 3: Broader search for non-Tesla EV battery sensors (BYD, etc.)
        # BYD uses sensor.*_elec_percent; other integrations use *_battery_level, *_soc
        try:
            for state in self.hass.states.async_all():
                entity_id = state.entity_id
                if not entity_id.startswith("sensor."):
                    continue
                entity_lower = entity_id.lower()
                # Match common EV battery patterns, skip Tesla (already checked above)
                # and skip home battery sensors (powerwall, battery_power, etc.)
                if any(p in entity_lower for p in ("_elec_percent", "_battery_level", "_charge_level", "_soc")):
                    # Skip known non-EV sensors
                    if any(skip in entity_lower for skip in (
                        "powerwall", "power_sync", "battery_power", "battery_range",
                        "phone", "tablet", "laptop", "watch",
                    )):
                        continue
                    if state.state not in ("unavailable", "unknown", "None", None):
                        try:
                            level = float(state.state)
                            if 0 <= level <= 100:
                                _LOGGER.debug(f"Found EV battery level from generic sensor {entity_id}: {level}%")
                                return int(level)
                        except (ValueError, TypeError):
                            continue
        except Exception as e:
            _LOGGER.debug(f"Error in generic EV battery sensor search: {e}")

        _LOGGER.warning(f"Could not find EV battery level from any source (VIN: {vehicle_vin})")
        return None

    def _get_or_create_vehicle_state(self, vehicle_vin: str) -> PriceLevelChargingState:
        """Get or create per-vehicle charging state."""
        if vehicle_vin not in self._vehicle_states:
            self._vehicle_states[vehicle_vin] = PriceLevelChargingState()
        return self._vehicle_states[vehicle_vin]

    async def _start_charging(
        self,
        mode: str,
        reason: str,
        vehicle_vin: Optional[str] = None
    ) -> bool:
        """Start EV charging.

        Args:
            mode: Charging mode (e.g., "price_level_recovery")
            reason: Reason for starting charging
            vehicle_vin: Optional VIN for specific vehicle. If None, uses default.
        """
        # Zaptec standalone path — use Cloud API directly
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID, CONF_OCPP_ENABLED
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.resume_charging(charger_id)
                    # Update state
                    if vehicle_vin:
                        state = self._get_or_create_vehicle_state(vehicle_vin)
                        state.is_charging = True
                        state.charging_mode = mode
                        state.last_decision = "started"
                        state.last_decision_reason = reason
                    else:
                        self._state.is_charging = True
                        self._state.charging_mode = mode
                        self._state.last_decision = "started"
                        self._state.last_decision_reason = reason
                    _LOGGER.info(f"Price-level charging: Started Zaptec ({mode}) - {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"Price-level charging: Zaptec start failed: {e}")
                    return False

        # Determine charger type — OCPP or Tesla
        charger_type = "ocpp" if opts.get(CONF_OCPP_ENABLED) else "tesla"

        from .actions import _action_start_ev_charging_dynamic

        params = {
            "vehicle_vin": vehicle_vin,
            "dynamic_mode": "battery_target",
            "min_charge_amps": 5,
            "max_charge_amps": 32,
            "voltage": 230,
            "charger_type": charger_type,
            "no_grid_import": self._get_settings().get("no_grid_import", False),
        }

        try:
            success = await _action_start_ev_charging_dynamic(
                self.hass, self.config_entry, params, context=None
            )

            if success:
                # Update per-vehicle state if VIN provided, otherwise legacy state
                if vehicle_vin:
                    state = self._get_or_create_vehicle_state(vehicle_vin)
                    state.is_charging = True
                    state.charging_mode = mode
                    state.last_decision = "started"
                    state.last_decision_reason = reason
                    _LOGGER.info(f"Price-level charging: Started ({mode}) for VIN {vehicle_vin} - {reason}")
                else:
                    self._state.is_charging = True
                    self._state.charging_mode = mode
                    self._state.last_decision = "started"
                    self._state.last_decision_reason = reason
                    _LOGGER.info(f"Price-level charging: Started ({mode}) - {reason}")
                # Note: Notifications are sent by _action_start_ev_charging_dynamic
                return True
            else:
                _LOGGER.warning(f"Price-level charging: Failed to start - {reason}")
                return False

        except Exception as e:
            _LOGGER.error(f"Price-level charging: Error starting: {e}")
            return False

    async def _stop_charging(self, reason: str, vehicle_vin: Optional[str] = None) -> bool:
        """Stop EV charging.

        Args:
            reason: Reason for stopping charging
            vehicle_vin: Optional VIN for specific vehicle. If None, uses default.
        """
        # Zaptec standalone path — use Cloud API directly
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.stop_charging(charger_id)
                    if vehicle_vin:
                        state = self._get_or_create_vehicle_state(vehicle_vin)
                        state.is_charging = False
                        state.charging_mode = ""
                        state.last_decision = "stopped"
                        state.last_decision_reason = reason
                    else:
                        self._state.is_charging = False
                        self._state.charging_mode = ""
                        self._state.last_decision = "stopped"
                        self._state.last_decision_reason = reason
                    _LOGGER.info(f"Price-level charging: Stopped Zaptec - {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"Price-level charging: Zaptec stop failed: {e}")
                    return False

        # Tesla path
        from .actions import _action_stop_ev_charging_dynamic

        params = {"vehicle_id": vehicle_vin}

        try:
            await _action_stop_ev_charging_dynamic(self.hass, self.config_entry, params)

            # Update per-vehicle state if VIN provided, otherwise legacy state
            if vehicle_vin:
                state = self._get_or_create_vehicle_state(vehicle_vin)
                state.is_charging = False
                state.charging_mode = ""
                state.last_decision = "stopped"
                state.last_decision_reason = reason
                _LOGGER.info(f"Price-level charging: Stopped for VIN {vehicle_vin} - {reason}")
            else:
                self._state.is_charging = False
                self._state.charging_mode = ""
                self._state.last_decision = "stopped"
                self._state.last_decision_reason = reason
                _LOGGER.info(f"Price-level charging: Stopped - {reason}")
            # Note: Notifications are sent by _action_stop_ev_charging_dynamic
            return True

        except Exception as e:
            _LOGGER.error(f"Price-level charging: Error stopping: {e}")
            return False

    async def get_charging_decision(self, current_price_cents: Optional[float]) -> Tuple[bool, str, str]:
        """
        Get charging decision without taking action.

        Returns:
            Tuple of (should_charge, reason, mode)
            mode is "price_level_recovery" or "price_level_opportunity"
        """
        settings = self._get_settings()

        _LOGGER.debug(
            f"Price-level charging decision: enabled={settings.get('enabled')}, "
            f"price={current_price_cents}c, recovery_soc={settings.get('recovery_soc')}, "
            f"recovery_price={settings.get('recovery_price_cents')}c, "
            f"opportunity_price={settings.get('opportunity_price_cents')}c"
        )

        # Check if enabled
        if not settings.get("enabled", False):
            self._state.last_decision = "disabled"
            self._state.last_decision_reason = "Price-level charging is disabled"
            return False, "Price-level charging is disabled", ""

        # Check if vehicle is at home
        location = await get_ev_location(self.hass, self.config_entry)
        if location not in ("home", "unknown"):
            self._state.last_decision = "away"
            self._state.last_decision_reason = f"Vehicle not at home (location: {location})"
            return False, f"Vehicle not at home ({location})", ""

        # Check if vehicle is plugged in
        plugged_in = await is_ev_plugged_in(self.hass, self.config_entry)
        if not plugged_in:
            self._state.last_decision = "unplugged"
            self._state.last_decision_reason = "Vehicle not plugged in"
            return False, "Vehicle not plugged in", ""

        # Check minimum home battery SOC
        min_home_battery = settings.get("home_battery_minimum", 20)
        if min_home_battery > 0:
            home_battery_soc = await self._get_home_battery_soc()
            if home_battery_soc is not None and home_battery_soc < min_home_battery:
                reason = f"Home battery {home_battery_soc:.0f}% < {min_home_battery}% minimum"
                self._state.last_decision = "waiting"
                self._state.last_decision_reason = reason
                return False, reason, ""

        # Get current EV SoC
        ev_soc = await self._get_ev_soc()
        if ev_soc is None:
            self._state.last_decision = "waiting"
            self._state.last_decision_reason = "Could not get EV state of charge"
            return False, "Could not get EV state of charge", ""

        # Get price
        if current_price_cents is None:
            self._state.last_decision = "waiting"
            self._state.last_decision_reason = "No price data available"
            return False, "No price data available", ""

        recovery_soc = settings.get("recovery_soc", 40)
        recovery_price = settings.get("recovery_price_cents", 30)
        opportunity_price = settings.get("opportunity_price_cents", 10)

        # Recovery mode: Below recovery_soc, charge if price is low enough
        if ev_soc < recovery_soc:
            if current_price_cents <= recovery_price:
                reason = f"Recovery: EV {ev_soc}% < {recovery_soc}%, price {current_price_cents:.1f}c <= {recovery_price}c"
                self._state.last_decision = "wants_charge"
                self._state.last_decision_reason = reason
                return True, reason, "price_level_recovery"
            else:
                reason = f"Recovery: EV {ev_soc}% < {recovery_soc}%, but price {current_price_cents:.1f}c > {recovery_price}c"
                self._state.last_decision = "waiting"
                self._state.last_decision_reason = reason
                return False, reason, ""

        # Opportunity mode: Above recovery_soc, only charge if price is very low
        else:
            if current_price_cents <= opportunity_price:
                reason = f"Opportunity: EV {ev_soc}%, price {current_price_cents:.1f}c <= {opportunity_price}c"
                self._state.last_decision = "wants_charge"
                self._state.last_decision_reason = reason
                return True, reason, "price_level_opportunity"
            else:
                reason = f"EV {ev_soc}% >= {recovery_soc}%, price {current_price_cents:.1f}c > {opportunity_price}c"
                self._state.last_decision = "waiting"
                self._state.last_decision_reason = reason
                return False, reason, ""

    async def evaluate(self, current_price_cents: Optional[float]) -> None:
        """
        Evaluate charging decision (legacy method for standalone use).
        For coordinated mode, use get_charging_decision() instead.
        """
        should_charge, reason, mode = await self.get_charging_decision(current_price_cents)

        # Take action
        if should_charge and not self._state.is_charging:
            await self._start_charging(mode, reason)
        elif not should_charge and self._state.is_charging:
            await self._stop_charging(reason)
        else:
            self._state.last_decision = "charging" if self._state.is_charging else "waiting"
            self._state.last_decision_reason = reason

    async def get_charging_decision_for_vehicle(
        self,
        vehicle_vin: str,
        current_price_cents: Optional[float]
    ) -> Tuple[bool, str, str]:
        """
        Make charging decision for a specific vehicle.

        Args:
            vehicle_vin: VIN of the vehicle to evaluate
            current_price_cents: Current electricity price in cents

        Returns:
            Tuple of (should_charge, reason, mode)
            mode is "price_level_recovery" or "price_level_opportunity"
        """
        settings = self._get_settings()
        vehicle_state = self._get_or_create_vehicle_state(vehicle_vin)

        _LOGGER.debug(
            f"Price-level charging decision for VIN {vehicle_vin}: enabled={settings.get('enabled')}, "
            f"price={current_price_cents}c"
        )

        # Check if enabled
        if not settings.get("enabled", False):
            vehicle_state.last_decision = "disabled"
            vehicle_state.last_decision_reason = "Price-level charging is disabled"
            return False, "Price-level charging is disabled", ""

        # Check if vehicle is at home
        location = await get_ev_location(self.hass, self.config_entry, vehicle_vin)
        if location not in ("home", "unknown"):
            vehicle_state.last_decision = "away"
            vehicle_state.last_decision_reason = f"Vehicle not at home (location: {location})"
            return False, f"Vehicle not at home ({location})", ""

        # Check if vehicle is plugged in
        plugged_in = await is_ev_plugged_in(self.hass, self.config_entry, vehicle_vin)
        if not plugged_in:
            vehicle_state.last_decision = "unplugged"
            vehicle_state.last_decision_reason = "Vehicle not plugged in"
            return False, "Vehicle not plugged in", ""

        # Check minimum home battery SOC
        min_home_battery = settings.get("home_battery_minimum", 20)
        if min_home_battery > 0:
            home_battery_soc = await self._get_home_battery_soc()
            if home_battery_soc is not None and home_battery_soc < min_home_battery:
                reason = f"Home battery {home_battery_soc:.0f}% < {min_home_battery}% minimum"
                vehicle_state.last_decision = "waiting"
                vehicle_state.last_decision_reason = reason
                return False, reason, ""

        # Get current EV SoC
        ev_soc = await self._get_ev_soc(vehicle_vin)
        if ev_soc is None:
            vehicle_state.last_decision = "waiting"
            vehicle_state.last_decision_reason = "Could not get EV state of charge"
            return False, "Could not get EV state of charge", ""

        # Get price
        if current_price_cents is None:
            vehicle_state.last_decision = "waiting"
            vehicle_state.last_decision_reason = "No price data available"
            return False, "No price data available", ""

        recovery_soc = settings.get("recovery_soc", 40)
        recovery_price = settings.get("recovery_price_cents", 30)
        opportunity_price = settings.get("opportunity_price_cents", 10)

        # Recovery mode: Below recovery_soc, charge if price is low enough
        if ev_soc < recovery_soc:
            if current_price_cents <= recovery_price:
                reason = f"Recovery: EV {ev_soc}% < {recovery_soc}%, price {current_price_cents:.1f}c <= {recovery_price}c"
                vehicle_state.last_decision = "wants_charge"
                vehicle_state.last_decision_reason = reason
                return True, reason, "price_level_recovery"
            else:
                reason = f"Recovery: EV {ev_soc}% < {recovery_soc}%, but price {current_price_cents:.1f}c > {recovery_price}c"
                vehicle_state.last_decision = "waiting"
                vehicle_state.last_decision_reason = reason
                return False, reason, ""

        # Opportunity mode: Above recovery_soc, only charge if price is very low
        else:
            if current_price_cents <= opportunity_price:
                reason = f"Opportunity: EV {ev_soc}%, price {current_price_cents:.1f}c <= {opportunity_price}c"
                vehicle_state.last_decision = "wants_charge"
                vehicle_state.last_decision_reason = reason
                return True, reason, "price_level_opportunity"
            else:
                reason = f"EV {ev_soc}% >= {recovery_soc}%, price {current_price_cents:.1f}c > {opportunity_price}c"
                vehicle_state.last_decision = "waiting"
                vehicle_state.last_decision_reason = reason
                return False, reason, ""

    async def evaluate_all_vehicles(
        self,
        current_price_cents: Optional[float]
    ) -> Dict[str, Tuple[bool, str, str]]:
        """
        Evaluate charging decisions for all discovered vehicles.

        Args:
            current_price_cents: Current electricity price in cents

        Returns:
            Dict mapping VIN to (should_charge, reason, mode) tuple
        """
        vehicles = await discover_all_tesla_vehicles(self.hass, self.config_entry)
        results: Dict[str, Tuple[bool, str, str]] = {}

        if not vehicles:
            # No Tesla vehicles — check if Zaptec standalone is configured
            # and fall back to single-vehicle evaluation (no VIN needed)
            from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME
            opts = {**self.config_entry.data, **self.config_entry.options}
            if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
                decision = await self.get_charging_decision(current_price_cents)
                should_charge, reason, mode = decision
                pseudo_vin = "zaptec_standalone"
                results[pseudo_vin] = decision

                vehicle_state = self._get_or_create_vehicle_state(pseudo_vin)
                _LOGGER.debug(
                    f"Zaptec standalone decision: should_charge={should_charge}, reason={reason}"
                )

                if should_charge and not vehicle_state.is_charging:
                    await self._start_charging(mode, reason)
                elif not should_charge and vehicle_state.is_charging:
                    await self._stop_charging(reason)
                else:
                    vehicle_state.last_decision = "charging" if vehicle_state.is_charging else "waiting"
                    vehicle_state.last_decision_reason = reason

                return results

            # Also check OCPP
            from ..const import CONF_OCPP_ENABLED
            if opts.get(CONF_OCPP_ENABLED):
                decision = await self.get_charging_decision(current_price_cents)
                should_charge, reason, mode = decision
                pseudo_vin = "ocpp_charger"
                results[pseudo_vin] = decision

                vehicle_state = self._get_or_create_vehicle_state(pseudo_vin)
                _LOGGER.debug(
                    f"OCPP charger decision: should_charge={should_charge}, reason={reason}"
                )

                if should_charge and not vehicle_state.is_charging:
                    await self._start_charging(mode, reason)
                elif not should_charge and vehicle_state.is_charging:
                    await self._stop_charging(reason)
                else:
                    vehicle_state.last_decision = "charging" if vehicle_state.is_charging else "waiting"
                    vehicle_state.last_decision_reason = reason

                return results

            _LOGGER.debug("No Tesla vehicles discovered for multi-vehicle evaluation")
            return results

        for vehicle in vehicles:
            vin = vehicle["vin"]
            name = vehicle.get("name", vin)

            # Get charging decision for this vehicle
            decision = await self.get_charging_decision_for_vehicle(vin, current_price_cents)
            results[vin] = decision

            should_charge, reason, mode = decision
            vehicle_state = self._get_or_create_vehicle_state(vin)

            _LOGGER.debug(
                f"Multi-vehicle decision for {name} ({vin}): "
                f"should_charge={should_charge}, reason={reason}"
            )

            # Take action per vehicle
            if should_charge and not vehicle_state.is_charging:
                await self._start_charging(mode, reason, vehicle_vin=vin)
            elif not should_charge and vehicle_state.is_charging:
                await self._stop_charging(reason, vehicle_vin=vin)
            else:
                vehicle_state.last_decision = "charging" if vehicle_state.is_charging else "waiting"
                vehicle_state.last_decision_reason = reason

        return results

    def update_charging_state(self, is_charging: bool, mode: str = "", reason: str = "") -> None:
        """Update internal state when coordinator controls charging."""
        self._state.is_charging = is_charging
        self._state.charging_mode = mode if is_charging else ""
        self._state.last_decision = "charging" if is_charging else "waiting"
        if reason:
            self._state.last_decision_reason = reason

    def update_vehicle_charging_state(
        self,
        vehicle_vin: str,
        is_charging: bool,
        mode: str = "",
        reason: str = ""
    ) -> None:
        """Update per-vehicle state when coordinator controls charging."""
        state = self._get_or_create_vehicle_state(vehicle_vin)
        state.is_charging = is_charging
        state.charging_mode = mode if is_charging else ""
        state.last_decision = "charging" if is_charging else "waiting"
        if reason:
            state.last_decision_reason = reason

    def get_state(self) -> dict:
        """Get current state for API."""
        settings = self._get_settings()

        # Build per-vehicle state info
        vehicle_states = {}
        for vin, state in self._vehicle_states.items():
            vehicle_states[vin] = {
                "is_charging": state.is_charging,
                "charging_mode": state.charging_mode,
                "last_decision": state.last_decision,
                "last_decision_reason": state.last_decision_reason,
            }

        return {
            "enabled": settings.get("enabled", False),
            "is_charging": self._state.is_charging,
            "charging_mode": self._state.charging_mode,
            "last_decision": self._state.last_decision,
            "last_decision_reason": self._state.last_decision_reason,
            "settings": settings,
            "vehicle_states": vehicle_states,  # Per-vehicle state tracking
        }


# Global price-level charging executor instance
_price_level_executor: Optional[PriceLevelChargingExecutor] = None


def get_price_level_executor() -> Optional[PriceLevelChargingExecutor]:
    """Get the global price-level charging executor instance."""
    return _price_level_executor


def set_price_level_executor(executor: PriceLevelChargingExecutor) -> None:
    """Set the global price-level charging executor instance."""
    global _price_level_executor
    _price_level_executor = executor


# ============================================================================
# SCHEDULED CHARGING EXECUTOR
# ============================================================================

@dataclass
class ScheduledChargingState:
    """State for scheduled charging."""
    is_charging: bool = False
    last_decision: str = "idle"
    last_decision_reason: str = ""


class ScheduledChargingExecutor:
    """
    Executes scheduled charging based on time window and max price.

    Charges when:
    - Current time is within start_time - end_time window
    - Current price is <= max_price_cents
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
    ):
        from ..const import DOMAIN
        self.hass = hass
        self.config_entry = config_entry
        self._domain = DOMAIN
        self._state = ScheduledChargingState()

    def _get_settings(self) -> dict:
        """Get scheduled charging settings from store."""
        entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
        store = entry_data.get("automation_store")

        defaults = {
            "enabled": False,
            "start_time": "00:00",
            "end_time": "06:00",
            "max_price_cents": 30,
        }

        if store:
            stored_data = getattr(store, '_data', {}) or {}
            settings = stored_data.get("scheduled_charging", {})
            defaults.update(settings)

        return defaults

    def _is_in_time_window(self, start_time_str: str, end_time_str: str) -> bool:
        """Check if current time is within the scheduled window."""
        try:
            now = datetime.now()
            current_time = now.time()

            # Parse start and end times
            start_parts = start_time_str.split(":")
            end_parts = end_time_str.split(":")

            start_time = dt_time(int(start_parts[0]), int(start_parts[1]))
            end_time = dt_time(int(end_parts[0]), int(end_parts[1]))

            # Handle overnight windows (e.g., 22:00 - 06:00)
            if start_time <= end_time:
                # Same day window
                return start_time <= current_time <= end_time
            else:
                # Overnight window
                return current_time >= start_time or current_time <= end_time

        except Exception as e:
            _LOGGER.error(f"Error parsing time window: {e}")
            return False

    async def _start_charging(self, reason: str) -> bool:
        """Start EV charging."""
        # Zaptec standalone path
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID, CONF_OCPP_ENABLED
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.resume_charging(charger_id)
                    self._state.is_charging = True
                    self._state.last_decision = "started"
                    self._state.last_decision_reason = reason
                    _LOGGER.info(f"Scheduled charging: Started Zaptec - {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"Scheduled charging: Zaptec start failed: {e}")
                    return False

        # Determine charger type
        charger_type = "ocpp" if opts.get(CONF_OCPP_ENABLED) else "tesla"

        from .actions import _action_start_ev_charging_dynamic

        params = {
            "vehicle_vin": None,
            "dynamic_mode": "battery_target",
            "min_charge_amps": 5,
            "max_charge_amps": 32,
            "voltage": 230,
            "charger_type": charger_type,
        }

        try:
            success = await _action_start_ev_charging_dynamic(
                self.hass, self.config_entry, params, context=None
            )

            if success:
                self._state.is_charging = True
                self._state.last_decision = "started"
                self._state.last_decision_reason = reason
                _LOGGER.info(f"Scheduled charging: Started - {reason}")
                # Note: Notifications are sent by _action_start_ev_charging_dynamic
                return True
            else:
                _LOGGER.warning(f"Scheduled charging: Failed to start - {reason}")
                return False

        except Exception as e:
            _LOGGER.error(f"Scheduled charging: Error starting: {e}")
            return False

    async def _stop_charging(self, reason: str) -> bool:
        """Stop EV charging."""
        # Zaptec standalone path
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.stop_charging(charger_id)
                    self._state.is_charging = False
                    self._state.last_decision = "stopped"
                    self._state.last_decision_reason = reason
                    _LOGGER.info(f"Scheduled charging: Stopped Zaptec - {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"Scheduled charging: Zaptec stop failed: {e}")
                    return False

        from .actions import _action_stop_ev_charging_dynamic

        params = {"vehicle_id": None}

        try:
            await _action_stop_ev_charging_dynamic(self.hass, self.config_entry, params)
            self._state.is_charging = False
            self._state.last_decision = "stopped"
            self._state.last_decision_reason = reason
            _LOGGER.info(f"Scheduled charging: Stopped - {reason}")
            # Note: Notifications are sent by _action_stop_ev_charging_dynamic
            return True

        except Exception as e:
            _LOGGER.error(f"Scheduled charging: Error stopping: {e}")
            return False

    async def get_charging_decision(self, current_price_cents: Optional[float]) -> Tuple[bool, str, str]:
        """
        Get charging decision without taking action.

        Returns:
            Tuple of (should_charge, reason, mode)
            mode is "scheduled"
        """
        settings = self._get_settings()

        # Check if enabled
        if not settings.get("enabled", False):
            self._state.last_decision = "disabled"
            self._state.last_decision_reason = "Scheduled charging is disabled"
            return False, "Scheduled charging is disabled", ""

        # Check if vehicle is at home
        location = await get_ev_location(self.hass, self.config_entry)
        if location not in ("home", "unknown"):
            self._state.last_decision = "away"
            self._state.last_decision_reason = f"Vehicle not at home (location: {location})"
            return False, f"Vehicle not at home ({location})", ""

        # Check if vehicle is plugged in
        plugged_in = await is_ev_plugged_in(self.hass, self.config_entry)
        if not plugged_in:
            self._state.last_decision = "unplugged"
            self._state.last_decision_reason = "Vehicle not plugged in"
            return False, "Vehicle not plugged in", ""

        start_time = settings.get("start_time", "00:00")
        end_time = settings.get("end_time", "06:00")
        max_price = settings.get("max_price_cents", 30)

        # Check if in time window
        in_window = self._is_in_time_window(start_time, end_time)

        if not in_window:
            reason = f"Outside schedule ({start_time}-{end_time})"
            self._state.last_decision = "waiting"
            self._state.last_decision_reason = reason
            return False, reason, ""

        # In time window - check price
        if current_price_cents is None:
            # No price data - charge anyway during window
            reason = f"Scheduled: {start_time}-{end_time}, no price data"
            self._state.last_decision = "wants_charge"
            self._state.last_decision_reason = reason
            return True, reason, "scheduled"
        elif current_price_cents <= max_price:
            reason = f"Scheduled: {start_time}-{end_time}, price {current_price_cents:.1f}c <= {max_price}c"
            self._state.last_decision = "wants_charge"
            self._state.last_decision_reason = reason
            return True, reason, "scheduled"
        else:
            reason = f"Scheduled: {start_time}-{end_time}, but price {current_price_cents:.1f}c > {max_price}c"
            self._state.last_decision = "waiting"
            self._state.last_decision_reason = reason
            return False, reason, ""

    async def evaluate(self, current_price_cents: Optional[float]) -> None:
        """
        Evaluate charging decision (legacy method for standalone use).
        For coordinated mode, use get_charging_decision() instead.
        """
        should_charge, reason, mode = await self.get_charging_decision(current_price_cents)

        # Take action
        if should_charge and not self._state.is_charging:
            await self._start_charging(reason)
        elif not should_charge and self._state.is_charging:
            await self._stop_charging(reason)
        else:
            self._state.last_decision = "charging" if self._state.is_charging else "waiting"
            self._state.last_decision_reason = reason

    def update_charging_state(self, is_charging: bool, reason: str = "") -> None:
        """Update internal state when coordinator controls charging."""
        self._state.is_charging = is_charging
        self._state.last_decision = "charging" if is_charging else "waiting"
        if reason:
            self._state.last_decision_reason = reason

    def get_state(self) -> dict:
        """Get current state for API."""
        settings = self._get_settings()
        return {
            "enabled": settings.get("enabled", False),
            "is_charging": self._state.is_charging,
            "last_decision": self._state.last_decision,
            "last_decision_reason": self._state.last_decision_reason,
            "settings": settings,
        }


# Global scheduled charging executor instance
_scheduled_charging_executor: Optional[ScheduledChargingExecutor] = None


def get_scheduled_charging_executor() -> Optional[ScheduledChargingExecutor]:
    """Get the global scheduled charging executor instance."""
    return _scheduled_charging_executor


def set_scheduled_charging_executor(executor: ScheduledChargingExecutor) -> None:
    """Set the global scheduled charging executor instance."""
    global _scheduled_charging_executor
    _scheduled_charging_executor = executor


# ============================================================================
# EV CHARGING MODE COORDINATOR
# ============================================================================

@dataclass
class ChargingModeDecision:
    """Decision from a charging mode."""
    mode_name: str
    wants_charge: bool
    reason: str
    source: str  # e.g., "price_level_recovery", "scheduled", "smart_schedule"


class EVChargingModeCoordinator:
    """
    Coordinates multiple EV charging modes using OR logic.

    If ANY enabled mode says "charge", charging starts.
    Only stops when ALL enabled modes say "don't charge".
    """

    def __init__(
        self,
        hass: "HomeAssistant",
        config_entry: "ConfigEntry",
    ):
        from ..const import DOMAIN
        self.hass = hass
        self.config_entry = config_entry
        self._domain = DOMAIN
        self._is_charging = False
        self._active_modes: List[str] = []
        self._last_reason = ""

    async def _start_charging(self, modes: List[str], reason: str) -> bool:
        """Start EV charging."""
        # Zaptec standalone path — use Cloud API directly
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID, CONF_OCPP_ENABLED
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.resume_charging(charger_id)
                    self._is_charging = True
                    self._active_modes = modes
                    self._last_reason = reason
                    _LOGGER.info(f"EV Coordinator: Started Zaptec charging - modes: {modes}, reason: {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"EV Coordinator: Zaptec start charging failed: {e}")
                    return False

        # Determine charger type — OCPP or Tesla
        charger_type = "ocpp" if opts.get(CONF_OCPP_ENABLED) else "tesla"

        from .actions import _action_start_ev_charging_dynamic

        params = {
            "vehicle_vin": None,
            "dynamic_mode": "battery_target",
            "min_charge_amps": 5,
            "max_charge_amps": 32,
            "voltage": 230,
            "charger_type": charger_type,
        }

        try:
            success = await _action_start_ev_charging_dynamic(
                self.hass, self.config_entry, params, context=None
            )

            if success:
                self._is_charging = True
                self._active_modes = modes
                self._last_reason = reason
                _LOGGER.info(f"EV Coordinator: Started charging - modes: {modes}, reason: {reason}")
                # Note: Notifications are sent by _action_start_ev_charging_dynamic
                return True
            else:
                _LOGGER.warning(f"EV Coordinator: Failed to start charging")
                return False

        except Exception as e:
            _LOGGER.error(f"EV Coordinator: Error starting charging: {e}")
            return False

    async def _stop_charging(self, reason: str) -> bool:
        """Stop EV charging."""
        # Zaptec standalone path — use Cloud API directly
        from ..const import CONF_ZAPTEC_STANDALONE_ENABLED, CONF_ZAPTEC_USERNAME, CONF_ZAPTEC_CHARGER_ID
        opts = {**self.config_entry.data, **self.config_entry.options}
        if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            client = entry_data.get("zaptec_client")
            charger_id = opts.get(CONF_ZAPTEC_CHARGER_ID, "")
            if client and charger_id:
                try:
                    await client.stop_charging(charger_id)
                    self._is_charging = False
                    self._active_modes = []
                    self._last_reason = reason
                    _LOGGER.info(f"EV Coordinator: Stopped Zaptec charging - {reason}")
                    return True
                except Exception as e:
                    _LOGGER.error(f"EV Coordinator: Zaptec stop charging failed: {e}")
                    return False

        # Tesla path
        from .actions import _action_stop_ev_charging_dynamic

        params = {"vehicle_id": None}

        try:
            await _action_stop_ev_charging_dynamic(self.hass, self.config_entry, params)
            self._is_charging = False
            self._active_modes = []
            self._last_reason = reason
            _LOGGER.info(f"EV Coordinator: Stopped charging - {reason}")
            # Note: Notifications are sent by _action_stop_ev_charging_dynamic
            return True

        except Exception as e:
            _LOGGER.error(f"EV Coordinator: Error stopping charging: {e}")
            return False

    async def evaluate(
        self,
        live_status: dict,
        current_price_cents: Optional[float],
    ) -> None:
        """
        Evaluate all charging modes and coordinate start/stop.

        Uses OR logic: if ANY enabled mode wants to charge, charge.
        For Price-Level charging, evaluates all discovered vehicles independently.
        """
        _LOGGER.debug(
            f"EV Coordinator evaluating: price={current_price_cents}c, "
            f"currently_charging={self._is_charging}"
        )

        # Safety: do not start EV charging while force discharge/charge is active.
        # Charging the car while force-discharging the house battery is counterproductive.
        try:
            entry_data = self.hass.data.get(self._domain, {}).get(self.config_entry.entry_id, {})
            fd_state = entry_data.get("force_discharge_state", {})
            fc_state = entry_data.get("force_charge_state", {})
            if fd_state.get("active") or fc_state.get("active"):
                force_type = "discharge" if fd_state.get("active") else "charge"
                _LOGGER.debug(
                    "EV Coordinator: skipping — force %s active", force_type
                )
                return
        except Exception:
            pass  # Don't let force state check break EV evaluation

        # Get price-level executor for multi-vehicle evaluation
        price_level_exec = get_price_level_executor()
        scheduled_exec = get_scheduled_charging_executor()

        # Multi-vehicle evaluation for Price-Level charging
        # This handles per-vehicle start/stop directly
        if price_level_exec:
            vehicle_results = await price_level_exec.evaluate_all_vehicles(current_price_cents)

            # Log per-vehicle decisions
            for vin, (should_charge, reason, mode) in vehicle_results.items():
                _LOGGER.debug(
                    f"EV Coordinator: Vehicle {vin} - should_charge={should_charge}, "
                    f"mode={mode}, reason={reason}"
                )

            # Track if any vehicle is charging for coordinator state
            any_price_level_charging = any(
                should_charge for should_charge, _, _ in vehicle_results.values()
            )
        else:
            any_price_level_charging = False
            vehicle_results = {}

        # Scheduled charging (legacy single-vehicle behavior)
        decisions: List[ChargingModeDecision] = []
        if scheduled_exec:
            wants_charge, reason, source = await scheduled_exec.get_charging_decision(current_price_cents)
            decisions.append(ChargingModeDecision(
                mode_name="Scheduled",
                wants_charge=wants_charge,
                reason=reason,
                source=source,
            ))

        # Note: Smart Schedule (AutoScheduleExecutor) is handled separately
        # because it has per-vehicle settings and manages backup reserve

        # Log scheduled charging decision
        for d in decisions:
            _LOGGER.debug(
                f"EV Coordinator decision: {d.mode_name} wants_charge={d.wants_charge}, "
                f"reason={d.reason}"
            )

        # Combine decisions using OR logic
        # Price-level is handled per-vehicle above, so only check scheduled here
        modes_wanting_charge = [d for d in decisions if d.wants_charge]

        # Also include price-level in active modes if any vehicle is charging
        if any_price_level_charging:
            if not any(d.mode_name == "Price-Level" for d in modes_wanting_charge):
                # Add a synthetic decision for tracking
                modes_wanting_charge.append(ChargingModeDecision(
                    mode_name="Price-Level",
                    wants_charge=True,
                    reason="Per-vehicle charging active",
                    source="price_level_multi_vehicle",
                ))

        if modes_wanting_charge:
            # At least one mode wants to charge
            active_modes = [d.mode_name for d in modes_wanting_charge]
            combined_reason = " | ".join([d.reason for d in modes_wanting_charge])

            # For scheduled charging (single vehicle), start if not already charging
            scheduled_wanting = [d for d in decisions if d.wants_charge]
            if scheduled_wanting and not self._is_charging:
                await self._start_charging(active_modes, combined_reason)

            # Update executor states
            for d in decisions:
                if d.mode_name == "Scheduled" and scheduled_exec:
                    scheduled_exec.update_charging_state(True, combined_reason)

            self._active_modes = active_modes
            self._last_reason = combined_reason
            self._is_charging = True  # Track overall state

        else:
            # No mode wants to charge
            if self._is_charging and not any_price_level_charging:
                reasons = [d.reason for d in decisions if d.reason]
                combined_reason = " | ".join(reasons) if reasons else "No mode wants to charge"
                await self._stop_charging(combined_reason)

            # Update executor states
            if scheduled_exec:
                scheduled_exec.update_charging_state(False)

            if not any_price_level_charging:
                self._is_charging = False
                self._active_modes = []

    def get_state(self) -> dict:
        """Get coordinator state for API."""
        return {
            "is_charging": self._is_charging,
            "active_modes": self._active_modes,
            "last_reason": self._last_reason,
        }


# Global coordinator instance
_ev_charging_coordinator: Optional[EVChargingModeCoordinator] = None


def get_ev_charging_coordinator() -> Optional[EVChargingModeCoordinator]:
    """Get the global EV charging mode coordinator instance."""
    return _ev_charging_coordinator


def set_ev_charging_coordinator(coordinator: EVChargingModeCoordinator) -> None:
    """Set the global EV charging mode coordinator instance."""
    global _ev_charging_coordinator
    _ev_charging_coordinator = coordinator
