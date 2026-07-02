"""
EV Charging Coordinator for PowerSync.

Coordinates EV charging alongside battery optimization.
EV scheduling uses the same price and forecast signals as the battery optimizer.

Strategy:
1. The built-in optimizer schedules the battery
2. EV Coordinator reads price forecasts and battery schedule
3. Schedules EV charging during cheap periods when battery isn't charging
4. Avoids conflicts: if battery is charging, EV waits (unless excess solar)
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from ..const import (
    DOMAIN,
    CONF_ZAPTEC_STANDALONE_ENABLED,
    CONF_ZAPTEC_USERNAME,
    CONF_ZAPTEC_INSTALLATION_ID_CLOUD,
)
from ..automations.ev_ownership import (
    DEFAULT_VEHICLE_ID,
    can_claim_ev_ownership,
    claim_ev_ownership,
    get_active_ev_owner_mode,
    owner_family,
    record_ev_command,
    release_ev_ownership,
)

_LOGGER = logging.getLogger(__name__)
EV_COORDINATOR_OWNER_MODE = "ev_coordinator"


class EVChargingMode(Enum):
    """EV charging mode."""
    OFF = "off"
    SMART = "smart"  # Charge during cheap periods
    SOLAR_ONLY = "solar_only"  # Only charge from excess solar
    IMMEDIATE = "immediate"  # Charge immediately regardless of price
    SCHEDULED = "scheduled"  # User-defined schedule


class EVChargingState(Enum):
    """Current EV charging state."""
    NOT_CONNECTED = "not_connected"
    CONNECTED_IDLE = "connected_idle"
    CHARGING = "charging"
    WAITING_CHEAP_RATE = "waiting_cheap_rate"
    WAITING_SOLAR = "waiting_solar"
    WAITING_BATTERY = "waiting_battery"  # Battery is charging
    COMPLETE = "complete"
    ERROR = "error"


@dataclass
class EVConfig:
    """Configuration for an EV charger."""
    entity_id: str  # HA entity for the charger
    name: str
    max_charging_power_w: int = 7400  # Default 32A single phase
    min_charging_power_w: int = 1400  # Minimum to start charging
    target_soc: float = 0.8  # Target state of charge
    departure_time: str | None = None  # When car needs to be ready
    price_threshold: float | None = None  # Max $/kWh for smart charging
    priority: int = 1  # Lower = higher priority


@dataclass
class EVStatus:
    """Current status of an EV."""
    entity_id: str
    connected: bool
    soc: float | None
    charging: bool
    power_w: float
    state: EVChargingState
    estimated_completion: datetime | None = None


@dataclass
class ChargingWindow:
    """A window for EV charging."""
    start: datetime
    end: datetime
    price: float
    power_available_w: float
    is_solar: bool = False


class EVCoordinator:
    """
    Coordinate EV charging alongside battery optimization.

    Reads optimizer schedule and price forecasts, then schedules EV charging
    in optimal windows with dynamic power sharing.

    Key concept: Cheap electricity periods are optimal for BOTH battery AND EV.
    Rather than avoiding battery charging windows, we dynamically adjust EV
    charging amps to share the available grid capacity.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        ev_configs: list[EVConfig] | None = None,
        price_getter: Callable[[], Any] | None = None,
        battery_schedule_getter: Callable[[], Any] | None = None,
        solar_forecast_getter: Callable[[], list[float]] | None = None,
        grid_capacity_w: float = 7400,  # 32A single phase default
        config_entry: Any | None = None,
    ):
        """Initialize EV coordinator.

        Args:
            hass: Home Assistant instance
            ev_configs: List of EV charger configurations
            price_getter: Callback to get price forecasts
            battery_schedule_getter: Callback to get battery schedule from optimizer
            solar_forecast_getter: Callback to get solar production forecast
            grid_capacity_w: Total grid import capacity in watts (default 7400W = 32A @ 230V)
        """
        self.hass = hass
        self._ev_configs: list[EVConfig] = ev_configs or []
        self._get_prices = price_getter
        self._get_battery_schedule = battery_schedule_getter
        self._get_solar_forecast = solar_forecast_getter
        self._grid_capacity_w = grid_capacity_w
        self._config_entry = config_entry

        self._mode = EVChargingMode.SMART
        self._enabled = False
        self._running = False
        self._task: asyncio.Task | None = None

        # Status tracking
        self._ev_statuses: dict[str, EVStatus] = {}
        self._last_update: datetime | None = None
        self._charging_plan: list[ChargingWindow] = []
        self._current_charge_amps: dict[str, int] = {}  # Track current amps per charger

    @property
    def enabled(self) -> bool:
        """Check if EV coordination is enabled."""
        return self._enabled

    @property
    def mode(self) -> EVChargingMode:
        """Get current charging mode."""
        return self._mode

    def add_ev(self, config: EVConfig) -> None:
        """Add an EV charger to coordinate."""
        self._ev_configs.append(config)
        _LOGGER.info(f"Added EV charger: {config.name} ({config.entity_id})")

    def remove_ev(self, entity_id: str) -> None:
        """Remove an EV charger."""
        self._ev_configs = [c for c in self._ev_configs if c.entity_id != entity_id]
        self._ev_statuses.pop(entity_id, None)

    def set_mode(self, mode: EVChargingMode | str) -> None:
        """Set the charging mode."""
        if isinstance(mode, str):
            mode = EVChargingMode(mode)
        self._mode = mode
        _LOGGER.info(f"EV charging mode set to: {mode.value}")

    async def start(self) -> bool:
        """Start EV coordination."""
        if self._running:
            return True

        if not self._ev_configs:
            _LOGGER.warning("No EV chargers configured")
            return False

        self._enabled = True
        self._running = True
        self._task = self.hass.async_create_task(self._coordination_loop())
        _LOGGER.info(f"EV coordination started with {len(self._ev_configs)} charger(s)")
        return True

    async def stop(self) -> None:
        """Stop EV coordination."""
        self._enabled = False
        self._running = False

        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        _LOGGER.info("EV coordination stopped")

    async def _coordination_loop(self) -> None:
        """Main coordination loop - runs every 5 minutes."""
        while self._running:
            try:
                await self._update_ev_statuses()
                await self._evaluate_and_control()
                self._last_update = dt_util.now()

                await asyncio.sleep(300)  # 5 minutes

            except asyncio.CancelledError:
                break
            except Exception as e:
                _LOGGER.error(f"Error in EV coordination loop: {e}")
                await asyncio.sleep(60)

    async def _update_ev_statuses(self) -> None:
        """Update status of all configured EVs."""
        for config in self._ev_configs:
            status = await self._get_ev_status(config)
            self._ev_statuses[config.entity_id] = status

    async def _get_ev_status(self, config: EVConfig) -> EVStatus:
        """Get current status of an EV charger."""
        # Try to get state from various EV integrations
        state = self.hass.states.get(config.entity_id)

        if not state or state.state in ("unknown", "unavailable"):
            return EVStatus(
                entity_id=config.entity_id,
                connected=False,
                soc=None,
                charging=False,
                power_w=0,
                state=EVChargingState.NOT_CONNECTED,
            )

        # Parse state based on common EV charger integrations
        charging = state.state in ("charging", "on")
        connected = state.state not in ("disconnected", "off", "unavailable")

        # Get power from attributes
        power_w = 0.0
        for attr in ["power", "charging_power", "current_power"]:
            if attr in state.attributes:
                power_w = float(state.attributes[attr])
                # Convert kW to W if needed
                if power_w < 100:
                    power_w *= 1000
                break

        # Get SOC from attributes or related sensor
        soc = None
        for attr in ["soc", "battery_level", "state_of_charge"]:
            if attr in state.attributes:
                soc = float(state.attributes[attr])
                if soc > 1:
                    soc /= 100
                break

        # Determine state
        if not connected:
            ev_state = EVChargingState.NOT_CONNECTED
        elif charging:
            ev_state = EVChargingState.CHARGING
        elif soc and soc >= config.target_soc:
            ev_state = EVChargingState.COMPLETE
        else:
            ev_state = EVChargingState.CONNECTED_IDLE

        return EVStatus(
            entity_id=config.entity_id,
            connected=connected,
            soc=soc,
            charging=charging,
            power_w=power_w,
            state=ev_state,
        )

    async def _evaluate_and_control(self) -> None:
        """Evaluate charging conditions and control EVs."""
        if self._mode == EVChargingMode.OFF:
            return

        if self._mode == EVChargingMode.IMMEDIATE:
            await self._start_all_charging()
            return

        # Get price and battery schedule data
        prices = await self._get_price_data()
        battery_schedule = await self._get_battery_schedule_data()
        solar_forecast = await self._get_solar_data()

        # Find optimal charging windows with dynamic power sharing
        windows = self._find_charging_windows(
            prices, battery_schedule, solar_forecast,
            grid_capacity_w=self._grid_capacity_w,
        )
        self._charging_plan = windows

        # Find current window for power availability
        now = dt_util.now()
        current_window = None
        for window in windows:
            if window.start <= now < window.end:
                current_window = window
                break

        # Evaluate each EV
        for config in self._ev_configs:
            status = self._ev_statuses.get(config.entity_id)
            if not status or not status.connected:
                continue

            if status.soc and status.soc >= config.target_soc:
                await self._stop_charging(config, reason="target reached")
                continue

            # Check if we should charge now
            should_charge = self._should_charge_now(config, status, windows)

            # Get available power for dynamic amp adjustment
            available_power = current_window.power_available_w if current_window else config.max_charging_power_w

            if should_charge:
                if not status.charging:
                    # Start charging with calculated power
                    started = await self._start_charging(config, power_w=available_power)
                    if started:
                        self._ev_statuses[config.entity_id].state = EVChargingState.CHARGING
                else:
                    # Already charging - adjust amps if power changed significantly
                    current_amps = self._current_charge_amps.get(config.entity_id, 0)
                    new_amps = int(available_power / 230)
                    if abs(new_amps - current_amps) >= 2:  # Only adjust if change >= 2A
                        await self._set_charging_amps(config, new_amps)
                        self._current_charge_amps[config.entity_id] = new_amps
                        _LOGGER.debug(f"Adjusted {config.name} charging: {current_amps}A -> {new_amps}A")
            elif status.charging:
                stopped = await self._stop_charging(config, reason="waiting for cheap rate")
                if stopped:
                    self._ev_statuses[config.entity_id].state = EVChargingState.WAITING_CHEAP_RATE

    async def _get_price_data(self) -> list[dict]:
        """Get price forecast data."""
        if not self._get_prices:
            return []

        try:
            result = self._get_prices()
            if asyncio.iscoroutine(result):
                result = await result
            return result or []
        except Exception as e:
            _LOGGER.debug(f"Failed to get prices: {e}")
            return []

    async def _get_battery_schedule_data(self) -> list[dict]:
        """Get battery schedule from optimizer."""
        if not self._get_battery_schedule:
            return []

        try:
            result = self._get_battery_schedule()
            if asyncio.iscoroutine(result):
                result = await result
            return result or []
        except Exception as e:
            _LOGGER.debug(f"Failed to get battery schedule: {e}")
            return []

    async def _get_solar_data(self) -> list[float]:
        """Get solar production forecast."""
        if not self._get_solar_forecast:
            return []

        try:
            result = self._get_solar_forecast()
            if asyncio.iscoroutine(result):
                result = await result
            return result or []
        except Exception as e:
            _LOGGER.debug(f"Failed to get solar forecast: {e}")
            return []

    def _find_charging_windows(
        self,
        prices: list[dict],
        battery_schedule: list[dict],
        solar_forecast: list[float],
        grid_capacity_w: float = 7400,
    ) -> list[ChargingWindow]:
        """Find optimal charging windows with dynamic power sharing.

        Strategy:
        1. Cheap electricity periods are optimal for BOTH battery and EV
        2. Calculate available EV power = grid_capacity - battery_power + solar_surplus
        3. Dynamically adjust EV charging amps based on available power
        4. Prefer times with lowest import prices and highest solar
        """
        windows = []
        now = dt_util.now()
        interval = timedelta(minutes=5)

        # Build time-indexed battery power schedule (power in watts)
        battery_power_by_time: dict[datetime, float] = {}
        for action in battery_schedule:
            if isinstance(action, dict):
                if action.get("action") == "charge":
                    ts = action.get("timestamp")
                    if isinstance(ts, str):
                        ts = datetime.fromisoformat(ts)
                    if ts:
                        power_w = action.get("power_w", 5000)
                        battery_power_by_time[ts.replace(second=0, microsecond=0)] = power_w

        # Process price intervals
        for i, price_data in enumerate(prices):
            if isinstance(price_data, dict):
                price = price_data.get("perKwh", price_data.get("value", 0))
                if isinstance(price, (int, float)) and price > 100:
                    price = price / 100  # Convert cents to dollars

                start_time = price_data.get("startTime", price_data.get("time"))
                if isinstance(start_time, str):
                    start_time = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                elif not isinstance(start_time, datetime):
                    start_time = now + (i * interval)
            else:
                price = float(price_data) if isinstance(price_data, (int, float)) else 0
                start_time = now + (i * interval)

            end_time = start_time + interval

            # Skip past intervals
            if end_time < now:
                continue

            # Get battery charging power for this interval
            interval_key = start_time.replace(second=0, microsecond=0)
            battery_power_w = battery_power_by_time.get(interval_key, 0)

            # Get solar for this interval
            solar_w = 0.0
            if i < len(solar_forecast):
                solar_w = solar_forecast[i]
                if solar_w < 100:  # Likely in kW
                    solar_w *= 1000

            # Dynamic power sharing calculation:
            # - Solar can power both battery and EV (doesn't count against grid limit)
            # - Grid import is limited to grid_capacity_w
            # - EV gets: (grid_capacity - grid_used_by_battery) + excess_solar

            if solar_w > 0:
                # Solar available - calculate how much grid the battery needs
                grid_needed_by_battery = max(0, battery_power_w - solar_w)
                available_grid_for_ev = grid_capacity_w - grid_needed_by_battery
                excess_solar = max(0, solar_w - battery_power_w)
                power_available = available_grid_for_ev + excess_solar
            else:
                # No solar - share grid capacity with battery
                power_available = grid_capacity_w - battery_power_w

            # Clamp to reasonable range
            power_available = max(0, min(power_available, grid_capacity_w))

            if power_available > 1400:  # Minimum 6A to charge
                windows.append(ChargingWindow(
                    start=start_time,
                    end=end_time,
                    price=price,
                    power_available_w=power_available,
                    is_solar=solar_w > 1000,
                ))

        # Sort by price (cheapest first)
        windows.sort(key=lambda w: (w.price, -w.power_available_w))

        return windows

    def _should_charge_now(
        self,
        config: EVConfig,
        status: EVStatus,
        windows: list[ChargingWindow],
    ) -> bool:
        """Determine if EV should charge now."""
        now = dt_util.now()

        # Find current window
        current_window = None
        for window in windows:
            if window.start <= now < window.end:
                current_window = window
                break

        if not current_window:
            return False

        # Mode-specific logic
        if self._mode == EVChargingMode.SOLAR_ONLY:
            return current_window.is_solar and current_window.power_available_w > config.min_charging_power_w

        if self._mode == EVChargingMode.SMART:
            # Check price threshold if set
            if config.price_threshold and current_window.price > config.price_threshold:
                return False

            # Check if this is one of the cheapest windows
            # Get remaining energy needed
            if status.soc:
                # Assume 75kWh battery
                energy_needed_kwh = (config.target_soc - status.soc) * 75
            else:
                energy_needed_kwh = 50  # Assume needs ~50kWh

            # Calculate how many 5-min intervals needed
            # Use 0.85 efficiency factor: chargers lose ~15% to AC-DC conversion,
            # ramp-up time, and thermal throttling vs peak rated power
            charging_rate_kw = (config.max_charging_power_w / 1000) * 0.85
            intervals_needed = math.ceil((energy_needed_kwh / charging_rate_kw) * 12)  # 12 intervals per hour

            # Check if departure time requires charging now
            if config.departure_time:
                try:
                    departure = datetime.strptime(config.departure_time, "%H:%M")
                    departure = now.replace(
                        hour=departure.hour,
                        minute=departure.minute,
                        second=0,
                        microsecond=0,
                    )
                    if departure < now:
                        departure += timedelta(days=1)

                    intervals_until_departure = int((departure - now).total_seconds() / 300)

                    # If we don't have enough cheap windows before departure, charge now
                    cheap_windows_before_departure = [
                        w for w in windows
                        if w.start < departure and w.price <= current_window.price * 1.2
                    ]

                    if len(cheap_windows_before_departure) <= intervals_needed:
                        return True
                except ValueError:
                    pass

            # Check if current price is in cheapest 30% of available windows
            if windows:
                price_rank = sum(1 for w in windows if w.price < current_window.price)
                if price_rank / len(windows) <= 0.3:
                    return True

            return False

        return False

    def _is_zaptec_standalone(self) -> bool:
        """Check if Zaptec standalone mode is configured."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            opts = {**entry.data, **entry.options}
            if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED) and opts.get(CONF_ZAPTEC_USERNAME):
                return True
        return False

    def _get_zaptec_config(self) -> dict:
        """Get Zaptec standalone configuration from config entry."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            opts = {**entry.data, **entry.options}
            if opts.get(CONF_ZAPTEC_STANDALONE_ENABLED):
                return opts
        return {}

    def _get_zaptec_cached_state(self) -> dict:
        """Return the latest cached Zaptec standalone state."""
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            entry_data = self.hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
            cached_state = entry_data.get("zaptec_cached_state", {})
            if cached_state:
                return cached_state
        return {}

    def _ownership_vehicle_id(self, config: EVConfig) -> str:
        """Use the charger entity as this legacy coordinator's loadpoint id."""
        return config.entity_id or DEFAULT_VEHICLE_ID

    def _can_control_loadpoint(self, config: EVConfig, command: str) -> bool:
        """Return whether the optimizer EV coordinator may control this charger."""
        if self._config_entry is None:
            return True

        vehicle_id = self._ownership_vehicle_id(config)
        allowed, _lease_id, _lease, reason = can_claim_ev_ownership(
            self.hass,
            self._config_entry,
            vehicle_id,
            owner_mode=EV_COORDINATOR_OWNER_MODE,
        )
        if allowed:
            return True

        block_reason = reason or "another EV mode owns this loadpoint"
        _LOGGER.info(
            "EV optimizer coordinator: %s blocked for %s because %s",
            command,
            config.name,
            block_reason,
        )
        record_ev_command(
            self.hass,
            self._config_entry,
            vehicle_id,
            command=command,
            success=False,
            reason=block_reason,
        )
        return False

    def _claim_loadpoint(self, config: EVConfig, command: str, reason: str) -> None:
        """Claim this charger's loadpoint after a successful physical command."""
        if self._config_entry is None:
            return

        claim_ev_ownership(
            self.hass,
            self._config_entry,
            self._ownership_vehicle_id(config),
            owner_mode=EV_COORDINATOR_OWNER_MODE,
            command=command,
            reason=reason,
            extra={"charger_entity_id": config.entity_id},
        )

    def _release_loadpoint(self, config: EVConfig, command: str, reason: str) -> None:
        """Release this charger's loadpoint after stop or passive cleanup."""
        if self._config_entry is None:
            return

        release_ev_ownership(
            self.hass,
            self._config_entry,
            self._ownership_vehicle_id(config),
            reason=reason,
            command=command,
        )

    def _record_loadpoint_failure(self, config: EVConfig, command: str, reason: str) -> None:
        """Record a failed command for diagnostics."""
        if self._config_entry is None:
            return

        record_ev_command(
            self.hass,
            self._config_entry,
            self._ownership_vehicle_id(config),
            command=command,
            success=False,
            reason=reason,
        )

    def _legacy_action_amps_entity(self, config: EVConfig) -> str | None:
        """Find the number entity that controls charger amps, if one exists."""
        entity_id = config.entity_id
        candidates = [
            entity_id.replace("switch.", "number.").replace("_charger", "_charging_amps"),
        ]
        candidates.extend(
            entity_id.replace("switch.", "number.") + suffix
            for suffix in ("_amps", "_charging_amps", "_current", "_charging_current")
        )
        for number_entity in candidates:
            if self.hass.states.get(number_entity):
                return number_entity
        return None

    def _legacy_shared_action_params(self, config: EVConfig) -> dict[str, Any] | None:
        """Build params for the shared EV action layer when this config maps cleanly."""
        vehicle_id = self._ownership_vehicle_id(config)

        if self._is_zaptec_standalone():
            return {
                "charger_type": "zaptec",
                "vehicle_id": vehicle_id,
                "vehicle_vin": None,
            }

        domain = config.entity_id.split(".")[0]
        if domain == "switch":
            return {
                "charger_type": "generic",
                "vehicle_id": vehicle_id,
                "vehicle_vin": None,
                "charger_switch_entity": config.entity_id,
                "charger_amps_entity": self._legacy_action_amps_entity(config) or "",
            }

        if domain == "ocpp":
            return {
                "charger_type": "ha_native",
                "vehicle_id": vehicle_id,
                "vehicle_vin": None,
                "charger_entity_id": config.entity_id,
                "charger_domain": domain,
                "charger_amps_entity": self._legacy_action_amps_entity(config) or "",
            }

        params = {
            "charger_type": "ha_native",
            "vehicle_id": vehicle_id,
            "vehicle_vin": None,
            "charger_entity_id": config.entity_id,
            "charger_domain": domain,
            "charger_amps_entity": self._legacy_action_amps_entity(config) or "",
        }
        if domain == "zaptec":
            params["zaptec_installation_id"] = self._get_zaptec_installation_id(config.entity_id) or ""
        return params

    async def _set_shared_action_amps(self, config: EVConfig, amps: int) -> bool | None:
        """Set/start/stop via shared EV actions; None means unsupported config."""
        params = self._legacy_shared_action_params(config)
        if params is None:
            return None

        from ..automations.actions import _set_vehicle_amps

        return await _set_vehicle_amps(
            self.hass,
            self._config_entry,
            self._ownership_vehicle_id(config),
            amps,
            params,
        )

    def _shared_start_failure_reason(self, config: EVConfig) -> str:
        """Return a more useful reason for known shared-action start failures."""
        if self._is_zaptec_standalone():
            cached_state = self._get_zaptec_cached_state()
            zaptec_config = self._get_zaptec_config()
            if (
                cached_state.get("charger_operation_mode") == "connected_waiting"
                and not zaptec_config.get(CONF_ZAPTEC_INSTALLATION_ID_CLOUD, "")
            ):
                return "Zaptec waiting but installation current could not be set"
        return "physical start failed"

    def _owns_loadpoint(self, config: EVConfig) -> bool:
        """Return whether this coordinator currently owns the charger."""
        if self._config_entry is None:
            return True

        owner_mode = get_active_ev_owner_mode(
            self.hass,
            self._config_entry,
            self._ownership_vehicle_id(config),
        )
        return owner_family(owner_mode) == owner_family(EV_COORDINATOR_OWNER_MODE)

    async def _start_charging(self, config: EVConfig, power_w: float | None = None) -> bool:
        """Start EV charging with dynamic amp adjustment.

        Args:
            config: EV charger configuration
            power_w: Available power in watts (used to calculate amps)
        """
        if not self._can_control_loadpoint(config, "start_ev_coordinator"):
            return False

        entity_id = config.entity_id

        # Calculate optimal amps based on available power
        if power_w:
            # Assume 230V single phase by default
            voltage = 230
            target_amps = int(power_w / voltage)
            # Clamp to charger limits
            target_amps = max(6, min(target_amps, config.max_charging_power_w // voltage))
        else:
            target_amps = config.max_charging_power_w // 230

        _LOGGER.info(f"Starting EV charging: {config.name} at {target_amps}A ({power_w or config.max_charging_power_w}W)")

        try:
            shared_result = await self._set_shared_action_amps(config, target_amps)
            if shared_result is None:
                reason = "unsupported charger configuration"
                self._record_loadpoint_failure(config, "start_ev_coordinator", reason)
                return False
            if not shared_result:
                reason = self._shared_start_failure_reason(config)
                self._record_loadpoint_failure(config, "start_ev_coordinator", reason)
                return False

            self._current_charge_amps[entity_id] = target_amps
            self._claim_loadpoint(
                config,
                "start_ev_coordinator",
                f"{self._mode.value} charging",
            )
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to start EV charging: {e}")
            self._record_loadpoint_failure(config, "start_ev_coordinator", str(e))
            return False

    async def _set_charging_amps(self, config: EVConfig, amps: int) -> bool:
        """Set EV charging amps for dynamic power sharing.

        Args:
            config: EV charger configuration
            amps: Target charging amps
        """
        entity_id = config.entity_id

        # Only adjust a session we actually own — otherwise we would fight
        # another automation (e.g. a solar-surplus tracker) that owns the
        # loadpoint. Mirrors the ownership checks on the start/stop paths.
        if not self._owns_loadpoint(config):
            _LOGGER.debug(
                "Skipping amp adjustment for %s — loadpoint not owned by optimizer",
                entity_id,
            )
            return False

        # Clamp to charger limits and the 6A minimum, as the start path does.
        amps = max(6, min(int(amps), config.max_charging_power_w // 230))

        shared_result = await self._set_shared_action_amps(config, amps)
        if shared_result is not None:
            return shared_result
        _LOGGER.debug("No shared EV action method found to set charging amps for %s", entity_id)
        return False

    async def _stop_charging(self, config: EVConfig, reason: str = "stopped") -> bool:
        """Stop EV charging."""
        if not self._owns_loadpoint(config):
            _LOGGER.info(
                "EV optimizer coordinator: stop blocked for %s because it does not own the loadpoint",
                config.name,
            )
            if self._config_entry is not None:
                record_ev_command(
                    self.hass,
                    self._config_entry,
                    self._ownership_vehicle_id(config),
                    command="stop_ev_coordinator",
                    success=False,
                    reason="not owned by EV optimizer coordinator",
                )
            return False

        _LOGGER.info(f"Stopping EV charging: {config.name}")

        try:
            shared_result = await self._set_shared_action_amps(config, 0)
            if shared_result is None:
                self._record_loadpoint_failure(
                    config,
                    "stop_ev_coordinator",
                    "unsupported charger configuration",
                )
                return False
            if not shared_result:
                self._record_loadpoint_failure(
                    config,
                    "stop_ev_coordinator",
                    "physical stop failed",
                )
                return False

            release_command = "stop_ev_coordinator"
            if self._is_zaptec_standalone():
                charger_mode = self._get_zaptec_cached_state().get(
                    "charger_operation_mode",
                    "",
                )
                if charger_mode in ("connected_waiting", "disconnected", ""):
                    release_command = "release"
            self._release_loadpoint(config, release_command, reason)
            return True
        except Exception as e:
            _LOGGER.error(f"Failed to stop EV charging: {e}")
            self._record_loadpoint_failure(config, "stop_ev_coordinator", str(e))
            return False

    async def _start_all_charging(self) -> None:
        """Start charging all connected EVs (immediate mode)."""
        for config in self._ev_configs:
            status = self._ev_statuses.get(config.entity_id)
            if status and status.connected and not status.charging:
                started = await self._start_charging(config)
                if started:
                    self._ev_statuses[config.entity_id].state = EVChargingState.CHARGING

    def _get_zaptec_installation_id(self, charger_entity_id: str) -> str | None:
        """Get the Zaptec installation device_id from config or device registry.

        The installation ID is needed because zaptec.limit_current targets the
        installation device, not the individual charger.
        """
        # Check if installation ID is stored in any PowerSync config entry
        for entry in self.hass.config_entries.async_entries("power_sync"):
            installation_id = entry.options.get(
                "zaptec_installation_id",
                entry.data.get("zaptec_installation_id"),
            )
            if installation_id:
                return installation_id

        # Fallback: scan device registry for a zaptec installation device
        try:
            from homeassistant.helpers import device_registry as dr
            device_registry = dr.async_get(self.hass)
            for device in device_registry.devices.values():
                for identifier in device.identifiers:
                    if identifier[0] == "zaptec" and "installation" in str(identifier[1]).lower():
                        return device.id
        except Exception as e:
            _LOGGER.debug(f"Failed to look up Zaptec installation: {e}")

        return None

    def get_status(self) -> dict[str, Any]:
        """Get EV coordination status for API."""
        return {
            "enabled": self._enabled,
            "mode": self._mode.value,
            "ev_count": len(self._ev_configs),
            "evs": [
                {
                    "entity_id": status.entity_id,
                    "connected": status.connected,
                    "soc": status.soc,
                    "charging": status.charging,
                    "power_w": status.power_w,
                    "state": status.state.value,
                    "estimated_completion": status.estimated_completion.isoformat() if status.estimated_completion else None,
                }
                for status in self._ev_statuses.values()
            ],
            "charging_plan": [
                {
                    "start": w.start.isoformat(),
                    "end": w.end.isoformat(),
                    "price": w.price,
                    "power_available_w": w.power_available_w,
                    "is_solar": w.is_solar,
                }
                for w in self._charging_plan[:24]  # Next 2 hours
            ],
            "last_update": self._last_update.isoformat() if self._last_update else None,
        }

    def get_next_charging_window(self) -> ChargingWindow | None:
        """Get the next optimal charging window."""
        now = dt_util.now()
        for window in self._charging_plan:
            if window.end > now:
                return window
        return None
