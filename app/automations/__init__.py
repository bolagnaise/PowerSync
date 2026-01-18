"""
Automation engine for PowerSync.

This module handles evaluation and execution of user-defined automations.
Automations consist of:
- A trigger (time, battery state, power flow, price, grid status, or weather)
- One or more actions (set backup reserve, force charge/discharge, etc.)
- Optional constraints (time window, run once, notification only)
"""

import logging
from datetime import datetime, time as dt_time
from typing import Optional, List, Dict, Any
import json

from app import db
from app.models import Automation, AutomationTrigger, AutomationAction, User

from .triggers import evaluate_trigger, TriggerResult
from .actions import execute_actions

_LOGGER = logging.getLogger(__name__)


class AutomationEngine:
    """Main automation engine that evaluates and executes automations."""

    def __init__(self):
        self._weather_cache: Dict[int, Dict[str, Any]] = {}  # user_id -> weather data
        self._weather_cache_time: Dict[int, datetime] = {}  # user_id -> cache timestamp

    def evaluate_all_automations(self) -> int:
        """
        Evaluate all enabled automations for all users.

        Returns:
            Number of automations that were triggered
        """
        triggered_count = 0

        # Get all enabled, non-paused automations
        automations = Automation.query.filter(
            Automation.enabled == True,
            Automation.paused == False
        ).order_by(Automation.priority.desc()).all()

        _LOGGER.debug(f"Found {len(automations)} enabled automations to evaluate")

        if not automations:
            _LOGGER.debug("No enabled automations found")
            return 0

        # Group by user for efficient state fetching
        user_automations: Dict[int, List[Automation]] = {}
        for automation in automations:
            if automation.user_id not in user_automations:
                user_automations[automation.user_id] = []
            user_automations[automation.user_id].append(automation)

        # Process each user's automations
        for user_id, user_autos in user_automations.items():
            user = User.query.get(user_id)
            if not user:
                continue

            # Get current state for this user (battery %, power flows, prices, etc.)
            try:
                current_state = self._get_current_state(user)
            except Exception as e:
                _LOGGER.error(f"Failed to get current state for user {user_id}: {e}")
                continue

            # Track which action types have been executed (for conflict resolution)
            executed_actions: set = set()

            # Evaluate each automation (already sorted by priority desc)
            for automation in user_autos:
                if not automation.trigger:
                    _LOGGER.warning(f"Automation '{automation.name}' (id={automation.id}) has no trigger configured - skipping")
                    continue

                try:
                    result = evaluate_trigger(automation.trigger, current_state, user)

                    if result.triggered:
                        _LOGGER.info(
                            f"Automation '{automation.name}' (id={automation.id}) triggered: {result.reason}"
                        )

                        # Execute actions (skip if higher priority automation already did same action)
                        actions_executed = self._execute_automation(
                            automation, user, executed_actions
                        )

                        if actions_executed:
                            triggered_count += 1

                            # Update last triggered timestamp
                            automation.last_triggered_at = datetime.utcnow()

                            # If run_once, pause the automation
                            if automation.run_once:
                                automation.paused = True
                                _LOGGER.info(
                                    f"Automation '{automation.name}' paused (run_once=True)"
                                )

                            db.session.commit()
                    else:
                        _LOGGER.debug(
                            f"Automation '{automation.name}' (id={automation.id}) not triggered: {result.reason}"
                        )

                except Exception as e:
                    _LOGGER.error(
                        f"Error evaluating automation '{automation.name}' (id={automation.id}): {e}"
                    )

        return triggered_count

    def _get_current_state(self, user: User) -> Dict[str, Any]:
        """
        Get current state for a user (battery, power flows, prices, etc.).

        This fetches live data from the user's configured battery system.
        Uses the user's timezone for time-based triggers.
        """
        from app.api_clients import get_tesla_client, AmberAPIClient
        from app.sigenergy_client import SigenergyAPIClient
        from zoneinfo import ZoneInfo

        # Get current time in user's timezone (for time-based automation triggers)
        user_tz = ZoneInfo(user.timezone) if user.timezone else ZoneInfo('Australia/Sydney')
        current_time_local = datetime.now(user_tz)

        state: Dict[str, Any] = {
            'battery_percent': None,
            'solar_power_kw': None,
            'grid_import_kw': None,
            'grid_export_kw': None,
            'home_usage_kw': None,
            'battery_charge_kw': None,
            'battery_discharge_kw': None,
            'import_price': None,
            'export_price': None,
            'grid_status': 'on_grid',  # 'on_grid' or 'off_grid'
            'weather': None,  # 'sunny', 'partly_sunny', 'cloudy'
            'current_time': current_time_local,  # User's local time for automation triggers
            'user_timezone': str(user_tz),  # Pass timezone info for logging
            'backup_reserve': None,
            'ev_vehicles': [],  # List of EV vehicle states
        }

        # Get battery/energy data based on user's battery system
        if user.battery_system == 'tesla':
            try:
                client = get_tesla_client(user)
                if client:
                    # Get live status
                    status = client.get_site_live_status(user.tesla_energy_site_id)
                    if status:
                        state['battery_percent'] = status.get('percentage_charged', 0)
                        state['backup_reserve'] = status.get('backup_reserve_percent')

                        solar_w = status.get('solar_power', 0) or 0
                        battery_w = status.get('battery_power', 0) or 0
                        grid_w = status.get('grid_power', 0) or 0
                        load_w = status.get('load_power', 0) or 0

                        state['solar_power_kw'] = solar_w / 1000
                        state['home_usage_kw'] = load_w / 1000

                        # Grid: positive = import, negative = export
                        if grid_w >= 0:
                            state['grid_import_kw'] = grid_w / 1000
                            state['grid_export_kw'] = 0
                        else:
                            state['grid_import_kw'] = 0
                            state['grid_export_kw'] = abs(grid_w) / 1000

                        # Battery: positive = discharge, negative = charge
                        if battery_w >= 0:
                            state['battery_discharge_kw'] = battery_w / 1000
                            state['battery_charge_kw'] = 0
                        else:
                            state['battery_charge_kw'] = abs(battery_w) / 1000
                            state['battery_discharge_kw'] = 0

                        # Grid status
                        grid_status = status.get('grid_status', 'Active')
                        state['grid_status'] = 'off_grid' if grid_status == 'Islanded' else 'on_grid'
            except Exception as e:
                _LOGGER.warning(f"Failed to get Tesla status for user {user.id}: {e}")

        elif user.battery_system == 'sigenergy':
            try:
                client = SigenergyAPIClient(user)
                status = client.get_live_status()
                if status:
                    state['battery_percent'] = status.get('battery_soc', 0)

                    solar_w = status.get('pv_power', 0) or 0
                    battery_w = status.get('battery_power', 0) or 0
                    grid_w = status.get('grid_power', 0) or 0
                    load_w = status.get('load_power', 0) or 0

                    state['solar_power_kw'] = solar_w / 1000
                    state['home_usage_kw'] = load_w / 1000

                    if grid_w >= 0:
                        state['grid_import_kw'] = grid_w / 1000
                        state['grid_export_kw'] = 0
                    else:
                        state['grid_import_kw'] = 0
                        state['grid_export_kw'] = abs(grid_w) / 1000

                    if battery_w >= 0:
                        state['battery_discharge_kw'] = battery_w / 1000
                        state['battery_charge_kw'] = 0
                    else:
                        state['battery_charge_kw'] = abs(battery_w) / 1000
                        state['battery_discharge_kw'] = 0
            except Exception as e:
                _LOGGER.warning(f"Failed to get Sigenergy status for user {user.id}: {e}")

        # Get current prices (Amber)
        if user.amber_api_token_encrypted and user.amber_site_id:
            try:
                from app.encryption import decrypt_token
                amber_token = decrypt_token(user.amber_api_token_encrypted)
                amber_client = AmberAPIClient(amber_token)
                prices = amber_client.get_current_prices(user.amber_site_id)

                if prices:
                    for price in prices:
                        channel = price.get('channelType', '')
                        per_kwh = price.get('perKwh', 0)

                        if channel == 'general':
                            state['import_price'] = per_kwh / 100  # Convert c/kWh to $/kWh
                        elif channel == 'feedIn':
                            state['export_price'] = abs(per_kwh) / 100
            except Exception as e:
                _LOGGER.warning(f"Failed to get Amber prices for user {user.id}: {e}")

        # Get weather (with caching)
        try:
            state['weather'] = self._get_weather_for_user(user)
        except Exception as e:
            _LOGGER.warning(f"Failed to get weather for user {user.id}: {e}")

        # Get EV vehicle states
        try:
            state['ev_vehicles'] = self._get_ev_vehicles_for_user(user)
        except Exception as e:
            _LOGGER.warning(f"Failed to get EV vehicles for user {user.id}: {e}")

        return state

    def _get_weather_for_user(self, user: User) -> Optional[str]:
        """
        Get current weather condition for a user (with 15-minute caching).

        Returns: 'sunny', 'partly_sunny', 'cloudy', or None
        """
        from .weather import get_current_weather

        cache_duration_seconds = 900  # 15 minutes

        # Check cache
        if user.id in self._weather_cache_time:
            cache_age = (datetime.utcnow() - self._weather_cache_time[user.id]).total_seconds()
            if cache_age < cache_duration_seconds and user.id in self._weather_cache:
                return self._weather_cache[user.id].get('condition')

        # Fetch fresh weather
        weather_data = get_current_weather(user)
        if weather_data:
            self._weather_cache[user.id] = weather_data
            self._weather_cache_time[user.id] = datetime.utcnow()
            return weather_data.get('condition')

        return None

    def _get_ev_vehicles_for_user(self, user: User) -> List[Dict[str, Any]]:
        """
        Get EV vehicle states for a user.

        Returns a list of vehicle state dictionaries containing:
        - id, vehicle_id, display_name
        - is_plugged_in, charging_state, battery_level
        - charge_limit_soc, charge_current_request
        """
        from app.models import TeslaVehicle
        from app.ev.tesla_fleet import get_fleet_client_for_user, sync_vehicles_for_user

        vehicles = TeslaVehicle.query.filter_by(
            user_id=user.id,
            enable_automations=True
        ).all()

        if not vehicles:
            return []

        # Check if we need to refresh vehicle data (refresh every 5 minutes)
        needs_refresh = False
        for v in vehicles:
            if v.data_updated_at is None:
                needs_refresh = True
                break
            age = (datetime.utcnow() - v.data_updated_at).total_seconds()
            if age > 300:  # 5 minutes
                needs_refresh = True
                break

        if needs_refresh:
            try:
                sync_vehicles_for_user(user)
                # Refresh the query after sync
                vehicles = TeslaVehicle.query.filter_by(
                    user_id=user.id,
                    enable_automations=True
                ).all()
            except Exception as e:
                _LOGGER.warning(f"Failed to sync EV vehicles: {e}")

        # Convert to state dictionaries
        ev_states = []
        for v in vehicles:
            ev_states.append({
                'id': v.id,
                'vehicle_id': v.vehicle_id,
                'display_name': v.display_name or v.vin or f'Vehicle {v.id}',
                'is_online': v.is_online,
                'is_plugged_in': v.is_plugged_in,
                'charging_state': v.charging_state,
                'battery_level': v.battery_level,
                'charge_limit_soc': v.charge_limit_soc,
                'charge_current_request': v.charge_current_request,
                'charger_power': v.charger_power,
            })

        return ev_states

    def _execute_automation(
        self,
        automation: Automation,
        user: User,
        executed_actions: set
    ) -> bool:
        """
        Execute an automation's actions.

        Always sends a notification when automation triggers.
        If notification_only is False, also executes the configured actions.

        Args:
            automation: The automation to execute
            user: The user who owns the automation
            executed_actions: Set of action types already executed by higher-priority automations

        Returns:
            True if automation was processed successfully
        """
        from app.push_notifications import send_push_notification

        # Always send notification when automation triggers
        message = f"Automation '{automation.name}' triggered"
        try:
            if user.apns_device_token:
                send_push_notification(user.apns_device_token, "PowerSync Automation", message, {
                    "type": "automation",
                    "automation_id": automation.id,
                    "automation_name": automation.name
                })
                _LOGGER.info(f"📱 Sent notification for automation '{automation.name}'")
            else:
                _LOGGER.warning(f"Cannot send notification - user {user.id} has no device token")
        except Exception as e:
            _LOGGER.warning(f"Failed to send notification: {e}")

        # If notification_only, we're done - don't execute actions
        if automation.notification_only:
            _LOGGER.info(f"Automation '{automation.name}' is notification-only, skipping actions")
            return True

        # Get actions sorted by execution order
        actions = automation.actions.order_by(AutomationAction.execution_order).all()

        # Filter out actions that conflict with higher-priority automations
        actions_to_execute = []
        for action in actions:
            if action.action_type in executed_actions:
                _LOGGER.debug(
                    f"Skipping action '{action.action_type}' for automation '{automation.name}' "
                    f"(already executed by higher-priority automation)"
                )
                continue
            actions_to_execute.append(action)
            executed_actions.add(action.action_type)

        if not actions_to_execute:
            _LOGGER.debug(f"Automation '{automation.name}' has no actions to execute")
            return True  # Still successful since notification was sent

        # Execute actions
        return execute_actions(actions_to_execute, user)


# Global engine instance
_engine: Optional[AutomationEngine] = None


def get_automation_engine() -> AutomationEngine:
    """Get the global automation engine instance."""
    global _engine
    if _engine is None:
        _engine = AutomationEngine()
    return _engine


def evaluate_automations() -> int:
    """
    Evaluate all automations (called by scheduler).

    Returns:
        Number of automations triggered
    """
    engine = get_automation_engine()
    return engine.evaluate_all_automations()
