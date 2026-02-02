"""
Grid Services and VPP (Virtual Power Plant) integration.

Integrates with VPP programs to:
1. Respond to grid events (price spikes, frequency regulation)
2. Participate in demand response programs
3. Optimize for VPP revenue while maintaining user preferences

Supported VPP programs:
- Amber Electric SmartShift
- AGL Virtual Power Plant
- Globird VPP (AEMO price spike response)
- Tesla Powerwall VPP
- Origin Energy Loop
- Reposit Power
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Awaitable

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class VPPProgram(Enum):
    """Supported VPP programs."""
    AMBER_SMARTSHIFT = "amber_smartshift"
    AGL_VPP = "agl_vpp"
    GLOBIRD_VPP = "globird_vpp"
    TESLA_VPP = "tesla_vpp"
    ORIGIN_LOOP = "origin_loop"
    REPOSIT = "reposit"
    GENERIC = "generic"


class GridEventType(Enum):
    """Types of grid events."""
    PRICE_SPIKE = "price_spike"           # High wholesale price
    NEGATIVE_PRICE = "negative_price"     # Negative wholesale price
    FREQUENCY_HIGH = "frequency_high"     # Grid frequency above threshold
    FREQUENCY_LOW = "frequency_low"       # Grid frequency below threshold
    DEMAND_RESPONSE = "demand_response"   # Demand response event
    EMERGENCY_RESERVE = "emergency_reserve"  # Emergency reserve activation
    FCAS_RAISE = "fcas_raise"             # FCAS raise service
    FCAS_LOWER = "fcas_lower"             # FCAS lower service


class GridEventResponse(Enum):
    """Response actions for grid events."""
    DISCHARGE_MAX = "discharge_max"       # Discharge at maximum rate
    DISCHARGE_PARTIAL = "discharge_partial"  # Discharge at partial rate
    CHARGE_MAX = "charge_max"             # Charge at maximum rate
    CHARGE_PARTIAL = "charge_partial"     # Charge at partial rate
    CURTAIL_LOAD = "curtail_load"         # Reduce household load
    CURTAIL_SOLAR = "curtail_solar"       # Reduce solar export
    NO_ACTION = "no_action"               # No response needed


@dataclass
class GridEvent:
    """A grid event that may require a response."""
    event_id: str
    event_type: GridEventType
    start_time: datetime
    end_time: datetime | None
    severity: int = 1                     # 1-5, higher = more severe
    price_threshold: float | None = None  # Price that triggered event
    current_price: float | None = None    # Current price
    region: str | None = None             # NEM region (NSW1, VIC1, etc.)
    source: VPPProgram = VPPProgram.GENERIC
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_minutes(self) -> int:
        """Get event duration in minutes."""
        if self.end_time:
            return int((self.end_time - self.start_time).total_seconds() / 60)
        return 30  # Default assumption

    @property
    def is_active(self) -> bool:
        """Check if event is currently active."""
        now = dt_util.now()
        if self.end_time:
            return self.start_time <= now <= self.end_time
        return self.start_time <= now


@dataclass
class VPPConfig:
    """Configuration for VPP participation."""
    program: VPPProgram
    enabled: bool = True

    # Participation limits
    max_export_kw: float = 5.0            # Max power to export for VPP
    min_reserve_soc: float = 0.30         # Minimum SOC to maintain during VPP events
    max_daily_cycles: float = 1.0         # Max additional cycles per day for VPP

    # Revenue settings
    vpp_export_bonus: float = 0.0         # Bonus $/kWh for VPP exports
    demand_response_payment: float = 0.0  # Payment for demand response $/kW

    # Thresholds
    price_spike_threshold: float = 1.0    # $/kWh - respond above this (configurable)
    negative_price_threshold: float = -0.05  # $/kWh - respond below this

    # Preferences
    auto_respond: bool = True             # Automatically respond to events
    notify_user: bool = True              # Notify user of events
    allow_grid_charging: bool = True      # Allow charging from grid during negative prices


@dataclass
class VPPResponse:
    """Response to a VPP event."""
    event_id: str
    response: GridEventResponse
    start_time: datetime
    end_time: datetime | None
    power_kw: float
    energy_kwh: float = 0.0
    revenue: float = 0.0
    status: str = "pending"


class GridServicesManager:
    """
    Manager for grid services and VPP participation.

    Monitors grid conditions, detects events, and coordinates responses
    with the battery optimization system.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        vpp_config: VPPConfig,
        battery_controller: Any = None,  # Battery controller interface
    ):
        """
        Initialize the grid services manager.

        Args:
            hass: Home Assistant instance
            vpp_config: VPP configuration
            battery_controller: Battery control interface
        """
        self.hass = hass
        self.config = vpp_config
        self.battery_controller = battery_controller

        # State
        self._active_events: dict[str, GridEvent] = {}
        self._event_history: list[GridEvent] = []
        self._response_history: list[VPPResponse] = []
        self._daily_vpp_cycles: float = 0.0
        self._last_cycle_reset: datetime | None = None

        # Callbacks
        self._event_callbacks: list[Callable[[GridEvent], Awaitable[None]]] = []

        # Monitoring
        self._last_price_check: datetime | None = None
        self._price_check_interval = timedelta(minutes=1)

    async def check_grid_conditions(self) -> list[GridEvent]:
        """
        Check current grid conditions and detect events.

        Returns:
            List of detected grid events
        """
        events = []

        # Check for price spikes
        price_event = await self._check_price_conditions()
        if price_event:
            events.append(price_event)

        # Check for AEMO events (if applicable)
        aemo_events = await self._check_aemo_events()
        events.extend(aemo_events)

        # Check for VPP program events
        vpp_events = await self._check_vpp_program_events()
        events.extend(vpp_events)

        # Update active events
        for event in events:
            if event.event_id not in self._active_events:
                self._active_events[event.event_id] = event
                self._event_history.append(event)
                await self._notify_event(event)

        # Clean up expired events
        await self._cleanup_expired_events()

        return events

    async def _check_price_conditions(self) -> GridEvent | None:
        """Check for price spike or negative price conditions."""
        try:
            from ..const import DOMAIN
            domain_data = self.hass.data.get(DOMAIN, {})

            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Look for price data
                price_data = entry_data.get("current_prices", {})
                if not price_data:
                    continue

                import_price = price_data.get("import_price")  # $/kWh
                if import_price is None:
                    continue

                now = dt_util.now()

                # Check for price spike
                if import_price >= self.config.price_spike_threshold:
                    return GridEvent(
                        event_id=f"price_spike_{now.strftime('%Y%m%d%H%M')}",
                        event_type=GridEventType.PRICE_SPIKE,
                        start_time=now,
                        end_time=now + timedelta(minutes=30),
                        severity=min(5, int((import_price / self.config.price_spike_threshold) * 2)),
                        price_threshold=self.config.price_spike_threshold,
                        current_price=import_price,
                        source=self.config.program,
                    )

                # Check for negative price
                if import_price <= self.config.negative_price_threshold:
                    return GridEvent(
                        event_id=f"negative_price_{now.strftime('%Y%m%d%H%M')}",
                        event_type=GridEventType.NEGATIVE_PRICE,
                        start_time=now,
                        end_time=now + timedelta(minutes=30),
                        severity=min(5, int(abs(import_price / self.config.negative_price_threshold))),
                        price_threshold=self.config.negative_price_threshold,
                        current_price=import_price,
                        source=self.config.program,
                    )

        except Exception as e:
            _LOGGER.debug(f"Error checking price conditions: {e}")

        return None

    async def _check_aemo_events(self) -> list[GridEvent]:
        """Check for AEMO NEM events (Australian market)."""
        events = []

        try:
            from ..const import DOMAIN
            domain_data = self.hass.data.get(DOMAIN, {})

            for entry_data in domain_data.values():
                if not isinstance(entry_data, dict):
                    continue

                # Look for AEMO data
                aemo_data = entry_data.get("aemo_prices", {})
                if not aemo_data:
                    continue

                now = dt_util.now()

                for region, data in aemo_data.items():
                    price_mwh = data.get("price", 0)
                    price_kwh = price_mwh / 1000

                    # AEMO price spike (>$3000/MWh = $3/kWh)
                    if price_kwh >= 3.0:
                        events.append(GridEvent(
                            event_id=f"aemo_spike_{region}_{now.strftime('%Y%m%d%H%M')}",
                            event_type=GridEventType.PRICE_SPIKE,
                            start_time=now,
                            end_time=now + timedelta(minutes=5),
                            severity=5,
                            current_price=price_kwh,
                            region=region,
                            source=VPPProgram.GLOBIRD_VPP,
                            metadata={"aemo_price_mwh": price_mwh},
                        ))

                    # AEMO negative price
                    elif price_kwh <= -0.1:
                        events.append(GridEvent(
                            event_id=f"aemo_negative_{region}_{now.strftime('%Y%m%d%H%M')}",
                            event_type=GridEventType.NEGATIVE_PRICE,
                            start_time=now,
                            end_time=now + timedelta(minutes=5),
                            severity=3,
                            current_price=price_kwh,
                            region=region,
                            source=VPPProgram.GLOBIRD_VPP,
                            metadata={"aemo_price_mwh": price_mwh},
                        ))

        except Exception as e:
            _LOGGER.debug(f"Error checking AEMO events: {e}")

        return events

    async def _check_vpp_program_events(self) -> list[GridEvent]:
        """Check for VPP program-specific events."""
        events = []

        # This would integrate with specific VPP APIs
        # For now, returns empty list as a placeholder

        if self.config.program == VPPProgram.AMBER_SMARTSHIFT:
            # Check Amber SmartShift events
            pass
        elif self.config.program == VPPProgram.TESLA_VPP:
            # Check Tesla VPP events
            pass

        return events

    async def respond_to_event(self, event: GridEvent) -> VPPResponse:
        """
        Generate and execute response to a grid event.

        Args:
            event: The grid event to respond to

        Returns:
            VPPResponse describing the action taken
        """
        if not self.config.enabled or not self.config.auto_respond:
            return VPPResponse(
                event_id=event.event_id,
                response=GridEventResponse.NO_ACTION,
                start_time=dt_util.now(),
                end_time=None,
                power_kw=0,
                status="disabled",
            )

        # Check daily cycle limit
        if self._daily_vpp_cycles >= self.config.max_daily_cycles:
            return VPPResponse(
                event_id=event.event_id,
                response=GridEventResponse.NO_ACTION,
                start_time=dt_util.now(),
                end_time=None,
                power_kw=0,
                status="cycle_limit_reached",
            )

        # Determine response based on event type
        response_type, power_kw = self._determine_response(event)

        if response_type == GridEventResponse.NO_ACTION:
            return VPPResponse(
                event_id=event.event_id,
                response=response_type,
                start_time=dt_util.now(),
                end_time=None,
                power_kw=0,
                status="no_action_needed",
            )

        # Execute response
        response = await self._execute_response(event, response_type, power_kw)
        self._response_history.append(response)

        return response

    def _determine_response(self, event: GridEvent) -> tuple[GridEventResponse, float]:
        """Determine appropriate response to an event."""
        if event.event_type == GridEventType.PRICE_SPIKE:
            # Discharge battery to capture high prices
            return GridEventResponse.DISCHARGE_MAX, self.config.max_export_kw

        elif event.event_type == GridEventType.NEGATIVE_PRICE:
            # Charge battery during negative prices
            if self.config.allow_grid_charging:
                return GridEventResponse.CHARGE_MAX, self.config.max_export_kw
            return GridEventResponse.NO_ACTION, 0

        elif event.event_type == GridEventType.DEMAND_RESPONSE:
            # Participate in demand response
            return GridEventResponse.DISCHARGE_PARTIAL, self.config.max_export_kw * 0.5

        elif event.event_type in [GridEventType.FCAS_RAISE, GridEventType.FCAS_LOWER]:
            # FCAS response (requires fast response capability)
            return GridEventResponse.NO_ACTION, 0  # Not implemented

        return GridEventResponse.NO_ACTION, 0

    async def _execute_response(
        self,
        event: GridEvent,
        response_type: GridEventResponse,
        power_kw: float,
    ) -> VPPResponse:
        """Execute the response action."""
        now = dt_util.now()

        if not self.battery_controller:
            return VPPResponse(
                event_id=event.event_id,
                response=response_type,
                start_time=now,
                end_time=event.end_time,
                power_kw=power_kw,
                status="no_controller",
            )

        try:
            duration_minutes = event.duration_minutes

            if response_type in [GridEventResponse.DISCHARGE_MAX, GridEventResponse.DISCHARGE_PARTIAL]:
                # Force discharge
                await self.battery_controller.force_discharge(
                    duration_minutes=duration_minutes,
                    power_w=int(power_kw * 1000),
                )
                self._daily_vpp_cycles += 0.1  # Approximate cycle usage

            elif response_type in [GridEventResponse.CHARGE_MAX, GridEventResponse.CHARGE_PARTIAL]:
                # Force charge
                await self.battery_controller.force_charge(
                    duration_minutes=duration_minutes,
                    power_w=int(power_kw * 1000),
                )
                self._daily_vpp_cycles += 0.1

            # Calculate estimated energy and revenue
            energy_kwh = power_kw * (duration_minutes / 60)
            revenue = 0.0

            if event.current_price and response_type in [GridEventResponse.DISCHARGE_MAX, GridEventResponse.DISCHARGE_PARTIAL]:
                revenue = energy_kwh * event.current_price
                if self.config.vpp_export_bonus > 0:
                    revenue += energy_kwh * self.config.vpp_export_bonus

            return VPPResponse(
                event_id=event.event_id,
                response=response_type,
                start_time=now,
                end_time=event.end_time,
                power_kw=power_kw,
                energy_kwh=energy_kwh,
                revenue=revenue,
                status="active",
            )

        except Exception as e:
            _LOGGER.error(f"Failed to execute VPP response: {e}")
            return VPPResponse(
                event_id=event.event_id,
                response=response_type,
                start_time=now,
                end_time=None,
                power_kw=0,
                status=f"error: {str(e)}",
            )

    async def _notify_event(self, event: GridEvent) -> None:
        """Notify callbacks of a new event."""
        for callback in self._event_callbacks:
            try:
                await callback(event)
            except Exception as e:
                _LOGGER.error(f"Event callback error: {e}")

    async def _cleanup_expired_events(self) -> None:
        """Remove expired events from active list."""
        now = dt_util.now()
        expired = [
            eid for eid, event in self._active_events.items()
            if event.end_time and event.end_time < now
        ]
        for eid in expired:
            del self._active_events[eid]

        # Reset daily cycle counter at midnight
        if self._last_cycle_reset is None or now.date() > self._last_cycle_reset.date():
            self._daily_vpp_cycles = 0.0
            self._last_cycle_reset = now

    def register_event_callback(
        self,
        callback: Callable[[GridEvent], Awaitable[None]],
    ) -> None:
        """Register a callback for grid events."""
        self._event_callbacks.append(callback)

    def get_active_events(self) -> list[GridEvent]:
        """Get list of currently active events."""
        return list(self._active_events.values())

    def get_event_history(self, days: int = 7) -> list[GridEvent]:
        """Get event history for the specified period."""
        cutoff = dt_util.now() - timedelta(days=days)
        return [e for e in self._event_history if e.start_time >= cutoff]

    def get_response_history(self, days: int = 7) -> list[VPPResponse]:
        """Get response history for the specified period."""
        cutoff = dt_util.now() - timedelta(days=days)
        return [r for r in self._response_history if r.start_time >= cutoff]

    def get_vpp_stats(self, days: int = 30) -> dict[str, Any]:
        """Get VPP participation statistics."""
        responses = self.get_response_history(days)

        total_energy = sum(r.energy_kwh for r in responses)
        total_revenue = sum(r.revenue for r in responses)
        event_count = len(responses)
        successful = sum(1 for r in responses if r.status == "active")

        return {
            "period_days": days,
            "total_events": event_count,
            "successful_responses": successful,
            "total_energy_kwh": round(total_energy, 2),
            "total_revenue": round(total_revenue, 2),
            "avg_revenue_per_event": round(total_revenue / event_count, 2) if event_count > 0 else 0,
            "daily_cycles_used": round(self._daily_vpp_cycles, 2),
            "daily_cycle_limit": self.config.max_daily_cycles,
        }


class VPPAwareOptimizer:
    """
    Battery optimizer that considers VPP events in scheduling.

    Extends the base optimizer to:
    1. Reserve capacity for anticipated VPP events
    2. Adjust schedules when events occur
    3. Maximize both energy arbitrage and VPP revenue
    """

    def __init__(
        self,
        base_optimizer,
        grid_services: GridServicesManager,
        vpp_config: VPPConfig,
    ):
        """
        Initialize VPP-aware optimizer.

        Args:
            base_optimizer: Base BatteryOptimizer or MultiBatteryOptimizer
            grid_services: Grid services manager
            vpp_config: VPP configuration
        """
        self.optimizer = base_optimizer
        self.grid_services = grid_services
        self.config = vpp_config

    async def optimize_with_vpp(
        self,
        prices_import: list[float],
        prices_export: list[float],
        solar_forecast: list[float],
        load_forecast: list[float],
        initial_soc: float,
        start_time: datetime,
        anticipated_events: list[GridEvent] | None = None,
    ):
        """
        Run optimization considering VPP events.

        Args:
            prices_import: Import prices
            prices_export: Export prices
            solar_forecast: Solar forecast
            load_forecast: Load forecast
            initial_soc: Initial battery SOC
            start_time: Start time
            anticipated_events: List of anticipated VPP events

        Returns:
            Optimization result with VPP considerations
        """
        # Modify prices to include VPP bonuses
        modified_export = list(prices_export)

        if anticipated_events:
            interval_minutes = self.optimizer.config.interval_minutes

            for event in anticipated_events:
                if event.event_type == GridEventType.PRICE_SPIKE:
                    # Find intervals during event
                    for i, t in enumerate(self._get_timestamps(start_time, len(prices_import), interval_minutes)):
                        if event.start_time <= t <= (event.end_time or event.start_time + timedelta(hours=1)):
                            # Add VPP bonus to export price
                            modified_export[i] += self.config.vpp_export_bonus

        # Adjust backup reserve if VPP events expected
        original_reserve = self.optimizer.config.backup_reserve
        if anticipated_events:
            # Ensure we have capacity for VPP response
            self.optimizer.config.backup_reserve = max(
                original_reserve,
                self.config.min_reserve_soc,
            )

        # Run optimization
        result = self.optimizer.optimize(
            prices_import=prices_import,
            prices_export=modified_export,
            solar_forecast=solar_forecast,
            load_forecast=load_forecast,
            initial_soc=initial_soc,
            start_time=start_time,
        )

        # Restore original reserve
        self.optimizer.config.backup_reserve = original_reserve

        return result

    def _get_timestamps(
        self,
        start_time: datetime,
        n_intervals: int,
        interval_minutes: int,
    ) -> list[datetime]:
        """Generate timestamps for intervals."""
        return [
            start_time + timedelta(minutes=interval_minutes * i)
            for i in range(n_intervals)
        ]
