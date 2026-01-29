"""
Automation engine for PowerSync Home Assistant integration.

This module handles evaluation and execution of user-defined automations.
Automations are stored using HA's Store helper for persistence.
"""

import logging
from datetime import datetime, time as dt_time
from typing import Optional, List, Dict, Any
import json

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .triggers import evaluate_trigger, evaluate_conditions, TriggerResult
from .actions import execute_actions

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "power_sync.automations"
STORAGE_VERSION = 1


class AutomationStore:
    """Manages automation storage using HA's Store helper."""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: Dict[str, Any] = {"automations": [], "next_id": 1, "push_tokens": {}}

    async def async_load(self) -> None:
        """Load automations from storage."""
        data = await self._store.async_load()
        if data:
            self._data = data
            # Ensure push_tokens exists (for upgrades from older versions)
            if "push_tokens" not in self._data:
                self._data["push_tokens"] = {}
        _LOGGER.debug(f"Loaded {len(self._data.get('automations', []))} automations from storage")
        _LOGGER.debug(f"Loaded {len(self._data.get('push_tokens', {}))} push tokens from storage")

    async def async_save(self) -> None:
        """Save automations to storage."""
        await self._store.async_save(self._data)

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all automations."""
        # Ensure automations key exists (recovery from corrupted state)
        if "automations" not in self._data:
            self._data["automations"] = []
        return self._data.get("automations", [])

    def get_by_id(self, automation_id: int) -> Optional[Dict[str, Any]]:
        """Get automation by ID."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                return auto
        return None

    def create(self, automation_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new automation."""
        # Ensure required keys exist (recovery from corrupted state)
        if "automations" not in self._data:
            self._data["automations"] = []
        if "next_id" not in self._data:
            self._data["next_id"] = 1

        automation_id = self._data.get("next_id", 1)
        self._data["next_id"] = automation_id + 1

        actions = automation_data.get("actions", [])
        _LOGGER.debug(f"Creating automation with {len(actions)} action(s): {actions}")

        conditions = automation_data.get("conditions", [])
        if conditions:
            _LOGGER.debug(f"Creating automation with {len(conditions)} condition(s)")

        automation = {
            "id": automation_id,
            "name": automation_data.get("name", "Unnamed Automation"),
            "group_name": automation_data.get("group_name", "Default Group"),
            "priority": automation_data.get("priority", 50),
            "enabled": automation_data.get("enabled", True),
            "run_once": automation_data.get("run_once", False),
            "paused": automation_data.get("paused", False),
            "notification_only": automation_data.get("notification_only", False),
            "trigger": automation_data.get("trigger", {}),
            "conditions": conditions,
            "actions": actions,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "last_triggered_at": None,
            "last_evaluated_value": None,
            "last_evaluated_at": None,
        }

        self._data["automations"].append(automation)
        return automation

    def update(self, automation_id: int, automation_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Update an existing automation."""
        for i, auto in enumerate(self._data.get("automations", [])):
            if auto.get("id") == automation_id:
                # Update fields
                if "name" in automation_data:
                    auto["name"] = automation_data["name"]
                if "group_name" in automation_data:
                    auto["group_name"] = automation_data["group_name"]
                if "priority" in automation_data:
                    auto["priority"] = automation_data["priority"]
                if "enabled" in automation_data:
                    auto["enabled"] = automation_data["enabled"]
                if "run_once" in automation_data:
                    auto["run_once"] = automation_data["run_once"]
                if "paused" in automation_data:
                    auto["paused"] = automation_data["paused"]
                if "notification_only" in automation_data:
                    auto["notification_only"] = automation_data["notification_only"]
                if "trigger" in automation_data:
                    auto["trigger"] = automation_data["trigger"]
                    # Reset trigger state when trigger config changes
                    # This allows re-evaluation with fresh state (e.g., after changing time window)
                    auto["last_evaluated_value"] = None
                    _LOGGER.debug(f"Reset trigger state for automation {automation_id} due to trigger config change")
                if "actions" in automation_data:
                    _LOGGER.debug(f"Updating automation {automation_id} with {len(automation_data['actions'])} action(s): {automation_data['actions']}")
                    auto["actions"] = automation_data["actions"]
                if "conditions" in automation_data:
                    _LOGGER.debug(f"Updating automation {automation_id} with {len(automation_data.get('conditions', []))} condition(s)")
                    auto["conditions"] = automation_data["conditions"]

                auto["updated_at"] = datetime.utcnow().isoformat() + "Z"
                self._data["automations"][i] = auto
                return auto
        return None

    def delete(self, automation_id: int) -> bool:
        """Delete an automation."""
        automations = self._data.get("automations", [])
        for i, auto in enumerate(automations):
            if auto.get("id") == automation_id:
                del automations[i]
                return True
        return False

    def toggle(self, automation_id: int) -> Optional[bool]:
        """Toggle automation enabled state."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["enabled"] = not auto.get("enabled", True)
                auto["updated_at"] = datetime.utcnow().isoformat() + "Z"
                return auto["enabled"]
        return None

    def pause(self, automation_id: int) -> bool:
        """Pause an automation."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["paused"] = True
                auto["updated_at"] = datetime.utcnow().isoformat() + "Z"
                return True
        return False

    def resume(self, automation_id: int) -> bool:
        """Resume a paused automation."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["paused"] = False
                auto["updated_at"] = datetime.utcnow().isoformat() + "Z"
                return True
        return False

    def update_trigger_state(self, automation_id: int, value: float) -> None:
        """Update trigger's last evaluated value."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["last_evaluated_value"] = value
                auto["last_evaluated_at"] = datetime.utcnow().isoformat() + "Z"
                break

    def mark_triggered(self, automation_id: int) -> None:
        """Mark automation as triggered."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["last_triggered_at"] = datetime.utcnow().isoformat() + "Z"
                if auto.get("run_once"):
                    auto["paused"] = True
                break

    def get_groups(self) -> List[str]:
        """Get unique group names."""
        groups = set()
        for auto in self._data.get("automations", []):
            group = auto.get("group_name")
            if group:
                groups.add(group)
        if "Default Group" not in groups:
            groups.add("Default Group")
        return sorted(list(groups))

    # Push token management (persisted to disk)
    def register_push_token(self, push_token: str, platform: str, device_name: str) -> None:
        """Register a push notification token."""
        if "push_tokens" not in self._data:
            self._data["push_tokens"] = {}

        self._data["push_tokens"][push_token] = {
            "token": push_token,
            "platform": platform,
            "device_name": device_name,
            "registered_at": datetime.utcnow().isoformat() + "Z",
        }
        _LOGGER.info(f"📱 Push token registered for {device_name} ({platform})")

    def get_push_tokens(self) -> Dict[str, Any]:
        """Get all registered push tokens."""
        return self._data.get("push_tokens", {})

    def remove_push_token(self, push_token: str) -> bool:
        """Remove a push token."""
        if "push_tokens" in self._data and push_token in self._data["push_tokens"]:
            del self._data["push_tokens"][push_token]
            return True
        return False

    # Custom tariff management (for non-Amber users)
    def get_custom_tariff(self) -> Optional[Dict[str, Any]]:
        """Get custom tariff configuration.

        Returns the custom tariff if configured, or None if not set.
        Custom tariffs are used by Globird/AEMO VPP/Other users who don't have
        live price APIs to define their TOU tariff structure.
        """
        return self._data.get("custom_tariff")

    def set_custom_tariff(self, tariff: Dict[str, Any]) -> None:
        """Save custom tariff configuration.

        Args:
            tariff: Custom tariff configuration following Tesla's tariff_content format.
                   Should include: name, utility, seasons, energy_charges, sell_tariff.
        """
        tariff["updated_at"] = datetime.utcnow().isoformat() + "Z"
        if "created_at" not in tariff:
            tariff["created_at"] = tariff["updated_at"]
        self._data["custom_tariff"] = tariff
        _LOGGER.info(f"Custom tariff saved: {tariff.get('name', 'Unnamed')}")

    def delete_custom_tariff(self) -> bool:
        """Delete custom tariff configuration.

        Returns True if a tariff was deleted, False if none existed.
        """
        if "custom_tariff" in self._data:
            del self._data["custom_tariff"]
            _LOGGER.info("Custom tariff deleted")
            return True
        return False

    # Vehicle SOC persistence (survives HA restarts)
    def get_vehicle_soc(self, vehicle_id: str) -> Optional[Dict[str, Any]]:
        """Get persisted vehicle SOC data.

        Returns dict with 'soc', 'updated_at', and optionally 'charging_state'.
        """
        vehicle_socs = self._data.get("vehicle_socs", {})
        return vehicle_socs.get(vehicle_id)

    def set_vehicle_soc(self, vehicle_id: str, soc: int, charging_state: Optional[str] = None) -> None:
        """Persist vehicle SOC data.

        Args:
            vehicle_id: Vehicle identifier (VIN or charger ID)
            soc: State of charge percentage (0-100)
            charging_state: Optional charging state (e.g., 'Charging', 'Complete', 'Disconnected')
        """
        if "vehicle_socs" not in self._data:
            self._data["vehicle_socs"] = {}

        self._data["vehicle_socs"][vehicle_id] = {
            "soc": soc,
            "charging_state": charging_state,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        _LOGGER.debug(f"Vehicle SOC persisted: {vehicle_id} = {soc}%")

    def get_all_vehicle_socs(self) -> Dict[str, Dict[str, Any]]:
        """Get all persisted vehicle SOC data."""
        return self._data.get("vehicle_socs", {})

    def clear_vehicle_soc(self, vehicle_id: str) -> bool:
        """Clear persisted SOC for a vehicle.

        Returns True if cleared, False if not found.
        """
        if "vehicle_socs" in self._data and vehicle_id in self._data["vehicle_socs"]:
            del self._data["vehicle_socs"][vehicle_id]
            return True
        return False


class AutomationEngine:
    """Main automation engine that evaluates and executes automations."""

    def __init__(self, hass: HomeAssistant, store: AutomationStore, config_entry):
        self._hass = hass
        self._store = store
        self._config_entry = config_entry
        self._weather_cache: Optional[Dict[str, Any]] = None
        self._weather_cache_time: Optional[datetime] = None
        self._tariff_cache: Optional[Dict[str, Any]] = None
        self._tariff_cache_time: Optional[datetime] = None

    async def async_evaluate_all(self) -> int:
        """
        Evaluate all enabled automations.

        Returns:
            Number of automations that were triggered
        """
        triggered_count = 0

        # Get all enabled, non-paused automations sorted by priority
        automations = [
            auto for auto in self._store.get_all()
            if auto.get("enabled") and not auto.get("paused")
        ]
        automations.sort(key=lambda x: x.get("priority", 50), reverse=True)

        if not automations:
            return 0

        # Get current state
        try:
            current_state = await self._async_get_current_state()
        except Exception as e:
            _LOGGER.error(f"Failed to get current state: {e}")
            return 0

        # Track executed action types for conflict resolution
        executed_actions: set = set()

        for automation in automations:
            trigger = automation.get("trigger", {})
            if not trigger:
                continue

            try:
                result = evaluate_trigger(
                    trigger,
                    current_state,
                    automation.get("last_evaluated_value"),
                    self._store,
                    automation.get("id")
                )

                if result.triggered:
                    _LOGGER.info(
                        f"Automation '{automation.get('name')}' (id={automation.get('id')}) triggered: {result.reason}"
                    )

                    # Check conditions (if any)
                    conditions = automation.get("conditions", [])
                    if conditions:
                        conditions_result = evaluate_conditions(conditions, current_state)
                        if not conditions_result.triggered:
                            _LOGGER.info(
                                f"Automation '{automation.get('name')}' conditions not met: {conditions_result.reason}"
                            )
                            continue  # Skip execution, conditions not met

                        _LOGGER.debug(f"Automation '{automation.get('name')}' conditions passed")

                    # Execute actions (pass current_state for time window context)
                    actions_executed = await self._async_execute_automation(
                        automation, executed_actions, current_state
                    )

                    if actions_executed:
                        triggered_count += 1
                        self._store.mark_triggered(automation.get("id"))
                        await self._store.async_save()

            except Exception as e:
                _LOGGER.error(
                    f"Error evaluating automation '{automation.get('name')}': {e}"
                )

        return triggered_count

    async def _async_get_current_state(self) -> Dict[str, Any]:
        """Get current state for automation evaluation."""
        from ..const import DOMAIN, CONF_AEMO_REGION
        from zoneinfo import ZoneInfo

        # NEM region to timezone mapping (for Sigenergy/AEMO users)
        NEM_REGION_TIMEZONES = {
            "NSW1": "Australia/Sydney",
            "VIC1": "Australia/Melbourne",
            "QLD1": "Australia/Brisbane",
            "SA1": "Australia/Adelaide",
            "TAS1": "Australia/Hobart",
        }

        # Default timezone - will be overridden by site info or NEM region
        user_timezone = self._config_entry.options.get(
            "timezone",
            self._config_entry.data.get("timezone", "Australia/Sydney")
        )

        # Try to get timezone from coordinator's site info (most accurate for Tesla)
        entry_id = self._config_entry.entry_id
        got_timezone_from_site_info = False
        if DOMAIN in self._hass.data and entry_id in self._hass.data[DOMAIN]:
            data = self._hass.data[DOMAIN][entry_id]
            # Coordinator is stored as "tesla_coordinator" for Tesla users
            coordinator = data.get("tesla_coordinator")
            if coordinator and hasattr(coordinator, 'async_get_site_info'):
                try:
                    site_info = await coordinator.async_get_site_info()
                    if site_info:
                        site_tz = site_info.get("installation_time_zone")
                        if site_tz:
                            user_timezone = site_tz
                            got_timezone_from_site_info = True
                            _LOGGER.debug(f"Using timezone from Tesla site info: {user_timezone}")
                except Exception as e:
                    _LOGGER.warning(f"Could not get timezone from site info: {e}")

        # Fallback: Try to get timezone from NEM region (for Sigenergy/AEMO users)
        if not got_timezone_from_site_info:
            nem_region = self._config_entry.options.get(
                CONF_AEMO_REGION,
                self._config_entry.data.get(CONF_AEMO_REGION)
            )
            if nem_region and nem_region in NEM_REGION_TIMEZONES:
                user_timezone = NEM_REGION_TIMEZONES[nem_region]
                _LOGGER.debug(f"Using timezone from NEM region ({nem_region}): {user_timezone}")

        # Get current time in user's timezone for time-based triggers
        try:
            user_tz = ZoneInfo(user_timezone)
            current_time_local = datetime.now(user_tz)
        except Exception:
            current_time_local = datetime.now()
            user_timezone = "UTC"

        state: Dict[str, Any] = {
            "battery_percent": None,
            "solar_power_kw": None,
            "grid_import_kw": None,
            "grid_export_kw": None,
            "home_usage_kw": None,
            "battery_charge_kw": None,
            "battery_discharge_kw": None,
            "import_price": None,
            "export_price": None,
            "grid_status": "on_grid",
            "weather": None,
            "current_time": current_time_local,
            "user_timezone": user_timezone,
            "backup_reserve": None,
            "ev_state": {},
            "ocpp_state": {},
            "solcast_forecast": {},
        }

        # Get data from coordinator
        entry_id = self._config_entry.entry_id
        if DOMAIN in self._hass.data and entry_id in self._hass.data[DOMAIN]:
            data = self._hass.data[DOMAIN][entry_id]
            # Use tesla_coordinator for Tesla users, sigenergy_coordinator for Sigenergy users
            coordinator = data.get("tesla_coordinator") or data.get("sigenergy_coordinator")

            if coordinator and coordinator.data:
                coord_data = coordinator.data
                _LOGGER.debug(f"Coordinator data keys: {list(coord_data.keys())}, battery_level={coord_data.get('battery_level')}")

                # Battery state
                state["battery_percent"] = coord_data.get("battery_level")
                state["backup_reserve"] = coord_data.get("backup_reserve_percent")

                # Power flows (convert W to kW)
                solar_w = coord_data.get("solar_power", 0) or 0
                battery_w = coord_data.get("battery_power", 0) or 0
                grid_w = coord_data.get("grid_power", 0) or 0
                load_w = coord_data.get("load_power", 0) or 0

                state["solar_power_kw"] = solar_w / 1000
                state["home_usage_kw"] = load_w / 1000

                # Grid: positive = import, negative = export
                if grid_w >= 0:
                    state["grid_import_kw"] = grid_w / 1000
                    state["grid_export_kw"] = 0
                else:
                    state["grid_import_kw"] = 0
                    state["grid_export_kw"] = abs(grid_w) / 1000

                # Battery: positive = discharge, negative = charge
                if battery_w >= 0:
                    state["battery_discharge_kw"] = battery_w / 1000
                    state["battery_charge_kw"] = 0
                else:
                    state["battery_charge_kw"] = abs(battery_w) / 1000
                    state["battery_discharge_kw"] = 0

                # Grid status
                grid_status = coord_data.get("grid_status", "Active")
                state["grid_status"] = "off_grid" if grid_status == "Islanded" else "on_grid"

                # Get prices for price-based triggers (settled prices from WebSocket or REST API)
                # - Amber users: Amber API prices
                # - Flow Power users: Either Amber or AEMO prices (based on flow_power_price_source)
                # - Globird users: Fixed prices, use time-based triggers instead
                amber_coord = data.get("amber_coordinator")
                aemo_coord = data.get("aemo_sensor_coordinator")

                if amber_coord and amber_coord.data:
                    # Amber API prices (for Amber users or Flow Power with Amber source)
                    current_prices = amber_coord.data.get("current", [])
                    for price in current_prices:
                        channel = price.get("channelType")
                        if channel == "general":
                            # Import price in $/kWh (Amber returns c/kWh)
                            state["import_price"] = price.get("perKwh", 0) / 100
                        elif channel == "feedIn":
                            # Export price: negate to show earnings (Amber feedIn is negative when earning)
                            state["export_price"] = -price.get("perKwh", 0) / 100
                elif aemo_coord and aemo_coord.data:
                    # AEMO wholesale prices (for Flow Power with AEMO source)
                    current_prices = aemo_coord.data.get("current", [])
                    for price in current_prices:
                        channel = price.get("channelType")
                        if channel == "general":
                            state["import_price"] = price.get("perKwh", 0) / 100
                        elif channel == "feedIn":
                            state["export_price"] = -price.get("perKwh", 0) / 100
                else:
                    # Fallback: Tesla tariff prices (for Globird users with Tesla)
                    # Uses the rate plan configured in the Tesla app
                    tariff_prices = await self._async_get_tesla_tariff_prices()
                    if tariff_prices:
                        state["import_price"] = tariff_prices.get("import_price")
                        state["export_price"] = tariff_prices.get("export_price")

        # Get EV state from Tesla Fleet entities
        try:
            state["ev_state"] = await self._async_get_ev_state()
        except Exception as e:
            _LOGGER.warning(f"Failed to get EV state: {e}")

        # Get OCPP state
        try:
            state["ocpp_state"] = await self._async_get_ocpp_state()
        except Exception as e:
            _LOGGER.warning(f"Failed to get OCPP state: {e}")

        # Get Solcast forecast
        try:
            state["solcast_forecast"] = await self._async_get_solcast_forecast()
        except Exception as e:
            _LOGGER.warning(f"Failed to get Solcast forecast: {e}")

        # Get weather
        try:
            state["weather"] = await self._async_get_weather()
        except Exception as e:
            _LOGGER.warning(f"Failed to get weather: {e}")

        return state

    async def _async_get_weather(self) -> Optional[str]:
        """Get current weather condition with caching."""
        from .weather import async_get_current_weather
        from ..const import CONF_OPENWEATHERMAP_API_KEY, CONF_WEATHER_LOCATION

        cache_duration_seconds = 900  # 15 minutes

        # Check cache
        if self._weather_cache_time:
            cache_age = (datetime.utcnow() - self._weather_cache_time).total_seconds()
            if cache_age < cache_duration_seconds and self._weather_cache:
                return self._weather_cache.get("condition")

        # Get API key from config
        api_key = self._config_entry.options.get(
            CONF_OPENWEATHERMAP_API_KEY,
            self._config_entry.data.get(CONF_OPENWEATHERMAP_API_KEY)
        )

        if not api_key:
            return None

        # Get weather location from config (city name or postcode)
        weather_location = self._config_entry.options.get(
            CONF_WEATHER_LOCATION,
            self._config_entry.data.get(CONF_WEATHER_LOCATION)
        )

        # Get timezone from config for location fallback
        timezone = self._config_entry.options.get(
            "timezone",
            self._config_entry.data.get("timezone", "Australia/Brisbane")
        )

        weather_data = await async_get_current_weather(self._hass, api_key, timezone, weather_location)
        if weather_data:
            self._weather_cache = weather_data
            self._weather_cache_time = datetime.utcnow()
            return weather_data.get("condition")

        return None

    async def _async_get_tesla_tariff_prices(self) -> Optional[Dict[str, float]]:
        """Get current prices from Tesla/custom tariff with real-time TOU calculation.

        Uses the rate plan configured in the Tesla app or custom tariff to calculate
        current TOU prices. Recalculates from TOU periods to ensure prices update
        when periods change (e.g., peak → off-peak transitions).
        Returns prices in $/kWh.
        """
        from ..const import DOMAIN
        from ..__init__ import get_current_price_from_tariff_schedule

        cache_duration_seconds = 60  # 1 minute (reduced for TOU accuracy)

        # Check cache (shorter duration for TOU accuracy)
        if self._tariff_cache_time:
            cache_age = (datetime.utcnow() - self._tariff_cache_time).total_seconds()
            if cache_age < cache_duration_seconds and self._tariff_cache:
                return self._tariff_cache

        entry_id = self._config_entry.entry_id
        if DOMAIN not in self._hass.data or entry_id not in self._hass.data[DOMAIN]:
            return None

        data = self._hass.data[DOMAIN][entry_id]

        try:
            # First, try stored tariff_schedule (from Tesla or custom tariff)
            tariff_schedule = data.get("tariff_schedule", {})
            if tariff_schedule:
                # Use real-time TOU calculation for accurate period pricing
                if tariff_schedule.get("tou_periods"):
                    buy_cents, sell_cents, current_period = get_current_price_from_tariff_schedule(tariff_schedule)
                    import_price = buy_cents / 100  # Convert cents to $/kWh
                    export_price = sell_cents / 100

                    result = {
                        "import_price": import_price,
                        "export_price": export_price,
                        "current_period": current_period,
                    }

                    self._tariff_cache = result
                    self._tariff_cache_time = datetime.utcnow()
                    _LOGGER.debug(f"Tariff prices (TOU calc): import=${import_price:.4f}, export=${export_price:.4f}, period={current_period}")

                    return result

                # Fallback to cached buy_price/sell_price if no TOU periods
                elif tariff_schedule.get("buy_price") is not None:
                    import_price = tariff_schedule.get("buy_price", 30.0) / 100
                    export_price = tariff_schedule.get("sell_price", 8.0) / 100

                    result = {
                        "import_price": import_price,
                        "export_price": export_price,
                    }

                    self._tariff_cache = result
                    self._tariff_cache_time = datetime.utcnow()
                    _LOGGER.debug(f"Tariff prices (cached): import=${import_price:.4f}, export=${export_price:.4f}")

                    return result

            # Fallback: Fetch fresh from Tesla API using the shared function
            from ..__init__ import fetch_tesla_tariff_schedule
            tariff_data = await fetch_tesla_tariff_schedule(self._hass, self._config_entry)

            if tariff_data:
                # Use real-time TOU calculation if available
                if tariff_data.get("tou_periods"):
                    buy_cents, sell_cents, current_period = get_current_price_from_tariff_schedule(tariff_data)
                    import_price = buy_cents / 100
                    export_price = sell_cents / 100
                else:
                    import_price = tariff_data.get("buy_price", 30.0) / 100
                    export_price = tariff_data.get("sell_price", 8.0) / 100

                result = {
                    "import_price": import_price,
                    "export_price": export_price,
                }

                self._tariff_cache = result
                self._tariff_cache_time = datetime.utcnow()
                _LOGGER.debug(f"Tariff prices (from API): import=${import_price:.4f}, export=${export_price:.4f}")

                return result

            return None

        except Exception as e:
            _LOGGER.debug(f"Error fetching tariff prices: {e}")
            return None

    async def _async_execute_automation(
        self,
        automation: Dict[str, Any],
        executed_actions: set,
        current_state: Dict[str, Any]
    ) -> bool:
        """Execute an automation's actions.

        If notification_only is True, sends a notification and skips actions.
        Otherwise, executes the configured actions (which may include send_notification).
        """
        automation_name = automation.get('name', 'Unnamed')

        # If notification_only, send notification and skip actions
        if automation.get("notification_only"):
            await self._async_send_notification(
                f"Automation '{automation_name}' triggered"
            )
            _LOGGER.info(f"Automation '{automation_name}' is notification-only, skipping actions")
            return True

        actions = automation.get("actions", [])
        _LOGGER.debug(f"Automation '{automation_name}' has {len(actions)} action(s): {[a.get('action_type') for a in actions]}")

        # Filter out conflicting actions
        actions_to_execute = []
        for action in actions:
            action_type = action.get("action_type")
            if action_type in executed_actions:
                _LOGGER.debug(
                    f"Skipping action '{action_type}' (already executed by higher-priority automation)"
                )
                continue
            actions_to_execute.append(action)
            executed_actions.add(action_type)

        if not actions_to_execute:
            _LOGGER.debug(f"Automation '{automation_name}' has no actions to execute")
            return True  # Still successful since notification was sent

        # Build context with time window info for EV charging actions
        trigger = automation.get("trigger", {})
        context = {
            "time_window_start": trigger.get("time_window_start"),
            "time_window_end": trigger.get("time_window_end"),
            "timezone": current_state.get("user_timezone", "UTC"),
        }

        result = await execute_actions(
            self._hass,
            self._config_entry,
            actions_to_execute,
            context
        )
        return result

    async def _async_send_notification(self, message: str) -> None:
        """Send notification via persistent notification and Expo Push."""
        # Send persistent notification (shows in HA UI)
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "PowerSync Automation",
                "message": message,
            },
        )

        # Send push notification to PowerSync mobile app via Expo Push API
        await self._async_send_expo_push(message)

    async def _async_send_expo_push(self, message: str, title: str = "PowerSync") -> None:
        """Send push notification via Expo Push API."""
        from ..const import DOMAIN
        import aiohttp

        # Get registered push tokens
        push_tokens = self._hass.data.get(DOMAIN, {}).get("push_tokens", {})
        if not push_tokens:
            _LOGGER.warning("📱 No push tokens registered, skipping push notification")
            return

        _LOGGER.info(f"📱 Found {len(push_tokens)} registered push token(s)")

        # Prepare messages for Expo Push API
        messages = []
        skipped_tokens = 0
        for token_data in push_tokens.values():
            token = token_data.get("token")
            platform = token_data.get("platform", "unknown")
            device = token_data.get("device_name", "unknown")
            if token and token.startswith("ExponentPushToken"):
                messages.append({
                    "to": token,
                    "title": title,
                    "body": message,
                    "sound": "default",
                    "priority": "high",
                })
                _LOGGER.debug(f"📱 Including token for {device} ({platform})")
            else:
                skipped_tokens += 1
                _LOGGER.warning(f"📱 Skipping non-Expo token for {device} ({platform}): {token[:30] if token else 'None'}...")

        if not messages:
            _LOGGER.warning(f"📱 No valid Expo push tokens found (skipped {skipped_tokens} invalid tokens)")
            return

        # Send to Expo Push API
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://exp.host/--/api/v2/push/send",
                    json=messages,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        _LOGGER.info(f"📱 Push notification sent to {len(messages)} device(s)")
                        _LOGGER.debug(f"Expo Push response: {result}")
                    else:
                        text = await response.text()
                        _LOGGER.error(f"Expo Push API error: {response.status} - {text}")
        except Exception as e:
            _LOGGER.error(f"Failed to send Expo push notification: {e}")

    async def _async_get_ev_state(self) -> Dict[str, Any]:
        """Get EV state from Tesla Fleet or other EV integrations."""
        import re

        ev_state = {
            "is_plugged_in": False,
            "is_charging": False,
            "battery_level": None,
            "charging_state": "",
            "location": "unknown",  # home, work, or zone name
        }

        # Look for Tesla Fleet entities
        # First, find Tesla EV by looking for _charging_state sensor (unique to Tesla Fleet)
        # Then use the same prefix to find related sensors
        all_states = self._hass.states.async_all()
        vehicle_prefix = None

        # First pass: find Tesla EV charging sensor to identify the vehicle prefix
        # Tesla Fleet uses: sensor.tessy_charging (no _state suffix)
        # Some versions use: sensor.tessy_charging_state
        for state in all_states:
            entity_id = state.entity_id
            # Try both patterns: _charging$ and _charging_state$
            match = re.match(r"sensor\.(\w+)_charging(?:_state)?$", entity_id)
            if match:
                vehicle_prefix = match.group(1)
                state_value = state.state
                if state_value not in ("unavailable", "unknown"):
                    ev_state["charging_state"] = state_value.lower()
                    ev_state["is_charging"] = state_value.lower() == "charging"
                    _LOGGER.debug(f"EV charging state from {entity_id}: {state_value}")
                break

        if not vehicle_prefix:
            _LOGGER.debug("No Tesla EV charging sensor found (sensor.*_charging or sensor.*_charging_state)")
            return ev_state

        # Check if this is a BLE integration (prefix contains "ble")
        is_ble = "ble" in vehicle_prefix.lower()
        if is_ble:
            # BLE only works when car is nearby, so assume "home" location
            ev_state["location"] = "home"
            _LOGGER.debug(f"Tesla BLE detected (prefix={vehicle_prefix}), assuming location=home")

        # Second pass: find related sensors using the vehicle prefix
        # Support Tesla Fleet, Teslemetry, and Tesla BLE naming conventions
        for state in all_states:
            entity_id = state.entity_id
            state_value = state.state

            if state_value in ("unavailable", "unknown"):
                continue

            # Charger binary sensor (plugged in)
            # Tesla Fleet: binary_sensor.tessy_charger
            # Teslemetry: binary_sensor.tessy_charge_cable
            # Tesla BLE: binary_sensor.tesla_ble_charge_flap
            if entity_id in (f"binary_sensor.{vehicle_prefix}_charger",
                            f"binary_sensor.{vehicle_prefix}_charge_cable",
                            f"binary_sensor.{vehicle_prefix}_charge_flap"):
                ev_state["is_plugged_in"] = state_value.lower() == "on"
                _LOGGER.debug(f"EV plugged in from {entity_id}: {state_value}")

            # Battery level sensor
            # Tesla Fleet: sensor.tessy_battery
            # Teslemetry: sensor.tessy_battery_level
            # Tesla BLE: sensor.tesla_ble_charge_level
            elif entity_id in (f"sensor.{vehicle_prefix}_battery",
                              f"sensor.{vehicle_prefix}_battery_level",
                              f"sensor.{vehicle_prefix}_charge_level"):
                try:
                    level = float(state_value)
                    if 0 <= level <= 100:
                        ev_state["battery_level"] = level
                        _LOGGER.debug(f"EV battery level from {entity_id}: {level}%")
                except (ValueError, TypeError):
                    pass

            # Device tracker for location
            # Tesla Fleet/Teslemetry: device_tracker.tessy_location
            # Tesla BLE: no location tracking (BLE is local only)
            elif entity_id == f"device_tracker.{vehicle_prefix}_location":
                ev_state["location"] = state_value.lower()
                _LOGGER.debug(f"EV location from {entity_id}: {state_value}")

            # Teslemetry: binary_sensor.*_located_at_home
            elif entity_id == f"binary_sensor.{vehicle_prefix}_located_at_home":
                if state_value.lower() == "on":
                    ev_state["location"] = "home"
                    _LOGGER.debug(f"EV at home from {entity_id}: {state_value}")

            # Teslemetry: binary_sensor.*_located_at_work
            elif entity_id == f"binary_sensor.{vehicle_prefix}_located_at_work":
                if state_value.lower() == "on" and ev_state["location"] != "home":
                    ev_state["location"] = "work"
                    _LOGGER.debug(f"EV at work from {entity_id}: {state_value}")

        # Infer plugged in from charging state if not already determined
        # If car is charging/stopped/complete, it must be plugged in
        if not ev_state["is_plugged_in"] and ev_state["charging_state"]:
            charging_implies_plugged = ev_state["charging_state"] in ("charging", "stopped", "complete", "starting")
            if charging_implies_plugged:
                ev_state["is_plugged_in"] = True
                _LOGGER.debug(f"Inferred is_plugged_in=True from charging_state={ev_state['charging_state']}")

        _LOGGER.debug(f"EV state collected: {ev_state}")
        return ev_state

    async def _async_get_ocpp_state(self) -> Dict[str, Any]:
        """Get OCPP charger state from OCPP integration entities."""
        import re

        ocpp_state = {
            "status": "",
            "is_connected": False,
            "energy_kwh": 0,
        }

        all_states = self._hass.states.async_all()

        for state in all_states:
            entity_id = state.entity_id
            state_value = state.state

            if state_value in ("unavailable", "unknown"):
                continue

            # OCPP status sensor (e.g., sensor.evse_status, sensor.*_charger_status)
            if re.match(r"sensor\.\w*(ocpp|evse|charger).*_status$", entity_id, re.IGNORECASE):
                ocpp_state["status"] = state_value.lower()
                ocpp_state["is_connected"] = state_value.lower() not in ("unavailable", "disconnected", "")
                _LOGGER.debug(f"OCPP status from {entity_id}: {state_value}")

            # OCPP energy sensor
            elif re.match(r"sensor\.\w*(ocpp|evse).*energy", entity_id, re.IGNORECASE):
                try:
                    ocpp_state["energy_kwh"] = float(state_value)
                except (ValueError, TypeError):
                    pass

        return ocpp_state

    async def _async_get_solcast_forecast(self) -> Dict[str, Any]:
        """Get Solcast solar forecast data from Solcast integration entities."""
        import re

        forecast = {
            "today_kwh": None,
            "tomorrow_kwh": None,
            "today_forecast_kwh": None,  # Alias for compatibility
        }

        all_states = self._hass.states.async_all()

        for state in all_states:
            entity_id = state.entity_id
            state_value = state.state

            if state_value in ("unavailable", "unknown"):
                continue

            # Solcast forecast today (e.g., sensor.solcast_pv_forecast_forecast_today)
            if re.match(r"sensor\.solcast.*forecast.*today", entity_id, re.IGNORECASE):
                try:
                    forecast["today_kwh"] = float(state_value)
                    forecast["today_forecast_kwh"] = float(state_value)
                    _LOGGER.debug(f"Solcast today forecast from {entity_id}: {state_value} kWh")
                except (ValueError, TypeError):
                    pass

            # Solcast forecast tomorrow
            elif re.match(r"sensor\.solcast.*forecast.*tomorrow", entity_id, re.IGNORECASE):
                try:
                    forecast["tomorrow_kwh"] = float(state_value)
                    _LOGGER.debug(f"Solcast tomorrow forecast from {entity_id}: {state_value} kWh")
                except (ValueError, TypeError):
                    pass

        return forecast
