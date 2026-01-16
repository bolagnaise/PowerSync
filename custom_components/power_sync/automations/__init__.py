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

from .triggers import evaluate_trigger, TriggerResult
from .actions import execute_actions

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = "power_sync.automations"
STORAGE_VERSION = 1


class AutomationStore:
    """Manages automation storage using HA's Store helper."""

    def __init__(self, hass: HomeAssistant):
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._data: Dict[str, Any] = {"automations": [], "next_id": 1}

    async def async_load(self) -> None:
        """Load automations from storage."""
        data = await self._store.async_load()
        if data:
            self._data = data
        _LOGGER.debug(f"Loaded {len(self._data.get('automations', []))} automations from storage")

    async def async_save(self) -> None:
        """Save automations to storage."""
        await self._store.async_save(self._data)

    def get_all(self) -> List[Dict[str, Any]]:
        """Get all automations."""
        return self._data.get("automations", [])

    def get_by_id(self, automation_id: int) -> Optional[Dict[str, Any]]:
        """Get automation by ID."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                return auto
        return None

    def create(self, automation_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create a new automation."""
        automation_id = self._data.get("next_id", 1)
        self._data["next_id"] = automation_id + 1

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
            "actions": automation_data.get("actions", []),
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
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
                if "actions" in automation_data:
                    auto["actions"] = automation_data["actions"]

                auto["updated_at"] = datetime.utcnow().isoformat()
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
                auto["updated_at"] = datetime.utcnow().isoformat()
                return auto["enabled"]
        return None

    def pause(self, automation_id: int) -> bool:
        """Pause an automation."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["paused"] = True
                auto["updated_at"] = datetime.utcnow().isoformat()
                return True
        return False

    def resume(self, automation_id: int) -> bool:
        """Resume a paused automation."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["paused"] = False
                auto["updated_at"] = datetime.utcnow().isoformat()
                return True
        return False

    def update_trigger_state(self, automation_id: int, value: float) -> None:
        """Update trigger's last evaluated value."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["last_evaluated_value"] = value
                auto["last_evaluated_at"] = datetime.utcnow().isoformat()
                break

    def mark_triggered(self, automation_id: int) -> None:
        """Mark automation as triggered."""
        for auto in self._data.get("automations", []):
            if auto.get("id") == automation_id:
                auto["last_triggered_at"] = datetime.utcnow().isoformat()
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


class AutomationEngine:
    """Main automation engine that evaluates and executes automations."""

    def __init__(self, hass: HomeAssistant, store: AutomationStore, config_entry):
        self._hass = hass
        self._store = store
        self._config_entry = config_entry
        self._weather_cache: Optional[Dict[str, Any]] = None
        self._weather_cache_time: Optional[datetime] = None

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

                    # Execute actions
                    actions_executed = await self._async_execute_automation(
                        automation, executed_actions
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
        from ..const import DOMAIN

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
            "current_time": datetime.now(),
            "backup_reserve": None,
        }

        # Get data from coordinator
        entry_id = self._config_entry.entry_id
        if DOMAIN in self._hass.data and entry_id in self._hass.data[DOMAIN]:
            data = self._hass.data[DOMAIN][entry_id]
            coordinator = data.get("coordinator")

            if coordinator and coordinator.data:
                coord_data = coordinator.data

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

                # Prices
                state["import_price"] = coord_data.get("current_import_price")
                state["export_price"] = coord_data.get("current_export_price")

        # Get weather
        try:
            state["weather"] = await self._async_get_weather()
        except Exception as e:
            _LOGGER.warning(f"Failed to get weather: {e}")

        return state

    async def _async_get_weather(self) -> Optional[str]:
        """Get current weather condition with caching."""
        from .weather import async_get_current_weather
        from ..const import CONF_OPENWEATHERMAP_API_KEY

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

        # Get timezone from config for location estimation
        timezone = self._config_entry.options.get(
            "timezone",
            self._config_entry.data.get("timezone", "Australia/Brisbane")
        )

        weather_data = await async_get_current_weather(self._hass, api_key, timezone)
        if weather_data:
            self._weather_cache = weather_data
            self._weather_cache_time = datetime.utcnow()
            return weather_data.get("condition")

        return None

    async def _async_execute_automation(
        self,
        automation: Dict[str, Any],
        executed_actions: set
    ) -> bool:
        """Execute an automation's actions."""
        if automation.get("notification_only"):
            # Only send notification
            await self._async_send_notification(
                f"Automation '{automation.get('name')}' triggered"
            )
            return True

        actions = automation.get("actions", [])

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
            return False

        return await execute_actions(
            self._hass,
            self._config_entry,
            actions_to_execute
        )

    async def _async_send_notification(self, message: str) -> None:
        """Send a persistent notification."""
        await self._hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "title": "PowerSync Automation",
                "message": message,
            },
        )
