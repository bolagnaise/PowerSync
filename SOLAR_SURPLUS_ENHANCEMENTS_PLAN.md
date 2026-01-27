# Solar Surplus EV Charging - Enhancement Plan

## Overview

This plan extends the Solar Surplus EV Charging feature with smart scheduling, forecasting, advanced strategies, and comprehensive mobile app control.

---

## Phase 1: Core Infrastructure & Data Model

### 1.1 Backend - Charging Session Tracking

**File:** `custom_components/power_sync/automations/ev_charging_session.py` (new)

```python
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List
from enum import Enum

class ChargingSource(Enum):
    SOLAR_SURPLUS = "solar_surplus"
    GRID_OFFPEAK = "grid_offpeak"
    GRID_PEAK = "grid_peak"
    MIXED = "mixed"

@dataclass
class ChargingSegment:
    """A continuous charging segment with consistent source."""
    start_time: datetime
    end_time: Optional[datetime] = None
    source: ChargingSource = ChargingSource.SOLAR_SURPLUS
    energy_kwh: float = 0.0
    avg_power_kw: float = 0.0
    avg_amps: int = 0
    solar_power_kwh: float = 0.0
    grid_power_kwh: float = 0.0
    cost_cents: float = 0.0
    savings_cents: float = 0.0  # vs grid charging

@dataclass
class ChargingSession:
    """A complete EV charging session from plug-in to plug-out."""
    id: str
    vehicle_id: str
    start_time: datetime
    end_time: Optional[datetime] = None

    # Energy tracking
    total_energy_kwh: float = 0.0
    solar_energy_kwh: float = 0.0
    grid_energy_kwh: float = 0.0
    solar_percentage: float = 0.0

    # Cost tracking
    total_cost_cents: float = 0.0
    grid_cost_avoided_cents: float = 0.0  # What it would have cost at grid rates
    export_revenue_lost_cents: float = 0.0  # FiT we didn't get
    net_savings_cents: float = 0.0

    # Vehicle state
    start_soc: Optional[int] = None
    end_soc: Optional[int] = None
    target_soc: Optional[int] = None

    # Session metadata
    mode: str = "solar_surplus"  # or "scheduled", "boost", "target_time"
    completed: bool = False
    stopped_reason: Optional[str] = None  # "target_reached", "unplugged", "manual", "time_window"

    # Detailed segments
    segments: List[ChargingSegment] = field(default_factory=list)

@dataclass
class ChargingSchedule:
    """Planned charging schedule for a vehicle."""
    vehicle_id: str
    target_soc: int
    target_time: Optional[datetime] = None  # Must be ready by this time
    min_soc: int = 20  # Never go below this

    # Strategy preferences
    prefer_solar: bool = True
    allow_grid_offpeak: bool = True
    allow_grid_peak: bool = False
    max_grid_cost_cents_kwh: Optional[float] = None

    # Calculated plan
    planned_windows: List[dict] = field(default_factory=list)
    estimated_solar_kwh: float = 0.0
    estimated_grid_kwh: float = 0.0
    estimated_cost_cents: float = 0.0
    confidence: float = 0.0  # 0-1, based on forecast reliability

class ChargingSessionManager:
    """Manages charging sessions and calculates statistics."""

    def __init__(self, hass, store):
        self.hass = hass
        self.store = store
        self.active_sessions: Dict[str, ChargingSession] = {}

    async def start_session(self, vehicle_id: str, mode: str, start_soc: Optional[int] = None) -> ChargingSession:
        """Start a new charging session."""
        pass

    async def update_session(self, vehicle_id: str, power_kw: float, source: ChargingSource, price_cents: float):
        """Update session with new power reading (called every 30s)."""
        pass

    async def end_session(self, vehicle_id: str, reason: str, end_soc: Optional[int] = None):
        """End a charging session and persist to storage."""
        pass

    async def get_session_history(self, vehicle_id: Optional[str] = None, days: int = 30) -> List[ChargingSession]:
        """Get historical charging sessions."""
        pass

    async def get_statistics(self, vehicle_id: Optional[str] = None, days: int = 30) -> dict:
        """Calculate charging statistics."""
        pass
```

### 1.2 Backend - Enhanced Vehicle Config

**File:** `custom_components/power_sync/automations/actions.py`

Extend `VehicleChargingConfig` to support scheduling:

```python
@dataclass
class VehicleChargingConfig:
    vehicle_id: str
    display_name: str

    # Charger settings
    charger_type: str  # tesla, ocpp, generic
    charger_switch_entity: Optional[str] = None
    charger_amps_entity: Optional[str] = None
    ocpp_charger_id: Optional[int] = None
    max_charger_power_kw: float = 7.0  # Charger limit

    # Charging limits
    min_amps: int = 5
    max_amps: int = 32
    voltage: int = 240
    phases: int = 1  # 1 or 3

    # Solar surplus settings
    solar_charging_enabled: bool = False
    priority: int = 1
    min_battery_soc: int = 80
    pause_below_soc: int = 70

    # Schedule settings
    schedule_enabled: bool = False
    default_target_soc: int = 80
    departure_time: Optional[str] = None  # "07:00" - default daily departure
    departure_days: List[int] = field(default_factory=lambda: [0,1,2,3,4])  # Mon-Fri

    # Price settings
    max_price_cents_kwh: Optional[float] = None  # Don't charge above this price
    prefer_export_over_ev: bool = False  # If FiT > charging cost, export instead

    # Notifications
    notify_on_start: bool = True
    notify_on_complete: bool = True
    notify_on_error: bool = True
```

### 1.3 Backend - API Endpoints

**File:** `custom_components/power_sync/__init__.py`

Add new endpoints:

```python
class ChargingSessionsView(HomeAssistantView):
    """GET /api/power_sync/ev/sessions - Get charging session history"""
    url = "/api/power_sync/ev/sessions"
    name = "api:power_sync:ev:sessions"

    async def get(self, request):
        vehicle_id = request.query.get("vehicle_id")
        days = int(request.query.get("days", 30))
        # Return session history with statistics

class ChargingStatisticsView(HomeAssistantView):
    """GET /api/power_sync/ev/statistics - Get charging statistics"""
    url = "/api/power_sync/ev/statistics"
    name = "api:power_sync:ev:statistics"

    async def get(self, request):
        # Return aggregated statistics

class ChargingScheduleView(HomeAssistantView):
    """GET/POST /api/power_sync/ev/schedule - Manage charging schedules"""
    url = "/api/power_sync/ev/schedule"
    name = "api:power_sync:ev:schedule"

    async def get(self, request):
        # Return current schedule and predicted windows

    async def post(self, request):
        # Update schedule (target SoC, departure time, etc.)

class ChargingBoostView(HomeAssistantView):
    """POST /api/power_sync/ev/boost - Trigger immediate boost charge"""
    url = "/api/power_sync/ev/boost"
    name = "api:power_sync:ev:boost"

    async def post(self, request):
        # Start immediate charging at max rate
```

### 1.4 Mobile App - Types

**File:** `PowerSyncMobile/src/types/evCharging.ts` (new)

```typescript
// Charging sources
export type ChargingSource = 'solar_surplus' | 'grid_offpeak' | 'grid_peak' | 'mixed';

// Charging session segment
export interface ChargingSegment {
  start_time: string;
  end_time?: string;
  source: ChargingSource;
  energy_kwh: number;
  avg_power_kw: number;
  avg_amps: number;
  solar_power_kwh: number;
  grid_power_kwh: number;
  cost_cents: number;
  savings_cents: number;
}

// Complete charging session
export interface ChargingSession {
  id: string;
  vehicle_id: string;
  start_time: string;
  end_time?: string;

  total_energy_kwh: number;
  solar_energy_kwh: number;
  grid_energy_kwh: number;
  solar_percentage: number;

  total_cost_cents: number;
  grid_cost_avoided_cents: number;
  export_revenue_lost_cents: number;
  net_savings_cents: number;

  start_soc?: number;
  end_soc?: number;
  target_soc?: number;

  mode: string;
  completed: boolean;
  stopped_reason?: string;

  segments: ChargingSegment[];
}

// Charging statistics
export interface ChargingStatistics {
  period_days: number;
  total_sessions: number;
  total_energy_kwh: number;
  solar_energy_kwh: number;
  grid_energy_kwh: number;
  solar_percentage: number;

  total_cost_dollars: number;
  total_savings_dollars: number;
  avg_cost_per_kwh_cents: number;

  avg_session_duration_minutes: number;
  avg_session_energy_kwh: number;

  by_vehicle: Record<string, {
    sessions: number;
    energy_kwh: number;
    solar_percentage: number;
    savings_dollars: number;
  }>;

  by_day: Array<{
    date: string;
    energy_kwh: number;
    solar_kwh: number;
    cost_cents: number;
  }>;
}

// Charging schedule
export interface ChargingSchedule {
  vehicle_id: string;
  target_soc: number;
  target_time?: string;
  min_soc: number;

  prefer_solar: boolean;
  allow_grid_offpeak: boolean;
  allow_grid_peak: boolean;
  max_grid_cost_cents_kwh?: number;

  planned_windows: PlannedChargingWindow[];
  estimated_solar_kwh: number;
  estimated_grid_kwh: number;
  estimated_cost_cents: number;
  confidence: number;
}

// Planned charging window
export interface PlannedChargingWindow {
  start_time: string;
  end_time: string;
  source: ChargingSource;
  estimated_power_kw: number;
  estimated_energy_kwh: number;
  price_cents_kwh: number;
  reason: string;  // "solar_forecast", "offpeak_rate", "target_deadline"
}

// Extended vehicle config
export interface VehicleChargingConfig {
  vehicle_id: string;
  display_name: string;

  // Charger settings
  charger_type: 'tesla' | 'ocpp' | 'generic';
  charger_switch_entity?: string;
  charger_amps_entity?: string;
  ocpp_charger_id?: number;
  max_charger_power_kw: number;

  // Charging limits
  min_amps: number;
  max_amps: number;
  voltage: number;
  phases: number;

  // Solar surplus settings
  solar_charging_enabled: boolean;
  priority: number;
  min_battery_soc: number;
  pause_below_soc: number;

  // Schedule settings
  schedule_enabled: boolean;
  default_target_soc: number;
  departure_time?: string;
  departure_days: number[];

  // Price settings
  max_price_cents_kwh?: number;
  prefer_export_over_ev: boolean;

  // Notifications
  notify_on_start: boolean;
  notify_on_complete: boolean;
  notify_on_error: boolean;
}
```

---

## Phase 2: Smart Scheduling with Forecasting

### 2.1 Backend - Charging Planner

**File:** `custom_components/power_sync/automations/ev_charging_planner.py` (new)

```python
class ChargingPlanner:
    """
    Plans optimal charging windows based on:
    - Solar forecast (Solcast)
    - Electricity prices (Amber/Flow Power)
    - Vehicle departure times
    - Current SoC and target SoC
    """

    def __init__(self, hass):
        self.hass = hass

    async def get_solar_forecast(self, hours: int = 24) -> List[dict]:
        """Get hourly solar forecast from Solcast."""
        # Returns: [{"hour": "2024-01-15T10:00", "pv_estimate_kw": 4.2}, ...]
        pass

    async def get_price_forecast(self, hours: int = 24) -> List[dict]:
        """Get hourly price forecast from Amber/configured provider."""
        # Returns: [{"hour": "...", "import_cents": 25, "export_cents": 8}, ...]
        pass

    async def get_surplus_forecast(self, hours: int = 24) -> List[dict]:
        """
        Combine solar forecast with typical load profile to estimate surplus.
        Uses historical data to predict household consumption patterns.
        """
        solar = await self.get_solar_forecast(hours)
        # Apply typical load curve (morning peak, midday low, evening peak)
        # Returns: [{"hour": "...", "estimated_surplus_kw": 2.1, "confidence": 0.7}, ...]
        pass

    async def plan_charging(
        self,
        vehicle_id: str,
        current_soc: int,
        target_soc: int,
        target_time: Optional[datetime],
        config: VehicleChargingConfig
    ) -> ChargingSchedule:
        """
        Create optimal charging plan.

        Strategy:
        1. Calculate energy needed: (target_soc - current_soc) * battery_capacity_kwh / 100
        2. Get surplus forecast windows
        3. Get price forecast
        4. Prioritize: surplus > offpeak > peak (if allowed)
        5. Work backwards from target_time to allocate windows
        6. Return plan with confidence level
        """
        pass

    async def should_charge_now(
        self,
        vehicle_id: str,
        schedule: ChargingSchedule,
        current_surplus_kw: float,
        current_price_cents: float,
        battery_soc: float
    ) -> Tuple[bool, str, ChargingSource]:
        """
        Real-time decision: should we charge right now?

        Returns: (should_charge, reason, source)
        """
        # Check if in planned window
        # Check if surplus available
        # Check price thresholds
        # Check battery priority
        pass
```

### 2.2 Backend - Forecast Integration

**File:** `custom_components/power_sync/automations/ev_charging_planner.py`

```python
class LoadProfileEstimator:
    """Estimates household load based on historical patterns."""

    def __init__(self, hass):
        self.hass = hass

    async def get_typical_load_profile(self, day_type: str = "weekday") -> List[float]:
        """
        Returns 24-hour load profile in kW based on historical data.
        day_type: "weekday" or "weekend"
        """
        # Query HA history for load_power sensor
        # Group by hour, calculate median
        # Apply day-of-week weighting
        pass

    async def estimate_load_at_hour(self, hour: datetime) -> Tuple[float, float]:
        """
        Returns (estimated_load_kw, confidence) for a specific hour.
        """
        pass

class SurplusForecaster:
    """Combines solar forecast with load estimation."""

    async def forecast_surplus(self, hours: int = 24) -> List[SurplusForecast]:
        """
        Returns hourly surplus forecast.
        surplus = solar_forecast - estimated_load - battery_charge_target
        """
        solar = await self.get_solar_forecast(hours)
        load = await self.load_estimator.get_typical_load_profile()

        forecasts = []
        for i, hour_data in enumerate(solar):
            hour = hour_data["hour"]
            pv_kw = hour_data["pv_estimate_kw"]
            load_kw = load[i % 24]

            # Reserve some for battery
            battery_reserve_kw = 1.0  # Keep 1kW for battery

            surplus_kw = max(0, pv_kw - load_kw - battery_reserve_kw)

            forecasts.append(SurplusForecast(
                hour=hour,
                solar_kw=pv_kw,
                load_kw=load_kw,
                surplus_kw=surplus_kw,
                confidence=hour_data.get("confidence", 0.7)
            ))

        return forecasts
```

### 2.3 Mobile App - Schedule Screen

**File:** `PowerSyncMobile/src/screens/EVScheduleScreen.tsx` (new)

```typescript
/**
 * EVScheduleScreen - Configure and view charging schedules
 *
 * Features:
 * - Set target SoC and departure time per vehicle
 * - View predicted charging windows (solar vs grid)
 * - See forecast confidence
 * - Override/boost controls
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, StyleSheet, ScrollView, TouchableOpacity,
  Switch, Alert, RefreshControl
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import Slider from '@react-native-community/slider';
import DateTimePicker from '@react-native-community/datetimepicker';
import { Colors, Spacing, BorderRadius, FontSize, CardShadow } from '../theme/colors';

interface Props {
  vehicleId: string;
}

export default function EVScheduleScreen({ vehicleId }: Props) {
  const [schedule, setSchedule] = useState<ChargingSchedule | null>(null);
  const [config, setConfig] = useState<VehicleChargingConfig | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [showTimePicker, setShowTimePicker] = useState(false);

  // Load schedule and config
  useEffect(() => {
    loadData();
  }, [vehicleId]);

  const loadData = async () => {
    // Fetch from API
  };

  const handleUpdateSchedule = async (updates: Partial<ChargingSchedule>) => {
    // Update via API
  };

  const handleBoostCharge = async () => {
    Alert.alert(
      'Boost Charge',
      'Start charging immediately at maximum rate?',
      [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Start', onPress: () => triggerBoost() }
      ]
    );
  };

  return (
    <ScrollView style={styles.container}>
      {/* Current Status Card */}
      <View style={styles.statusCard}>
        <View style={styles.statusRow}>
          <View style={styles.statusItem}>
            <Text style={styles.statusLabel}>Current</Text>
            <Text style={styles.statusValue}>{schedule?.current_soc || '--'}%</Text>
          </View>
          <Ionicons name="arrow-forward" size={24} color={Colors.textMuted} />
          <View style={styles.statusItem}>
            <Text style={styles.statusLabel}>Target</Text>
            <Text style={styles.statusValue}>{schedule?.target_soc || 80}%</Text>
          </View>
          <View style={styles.statusItem}>
            <Text style={styles.statusLabel}>By</Text>
            <Text style={styles.statusValue}>{config?.departure_time || '--:--'}</Text>
          </View>
        </View>

        {/* Progress bar */}
        <View style={styles.progressContainer}>
          <View style={[styles.progressBar, { width: `${schedule?.current_soc || 0}%` }]} />
          <View style={[styles.targetMarker, { left: `${schedule?.target_soc || 80}%` }]} />
        </View>

        {/* Confidence indicator */}
        <View style={styles.confidenceRow}>
          <Ionicons
            name={schedule?.confidence > 0.7 ? 'checkmark-circle' : 'alert-circle'}
            size={16}
            color={schedule?.confidence > 0.7 ? Colors.success : Colors.warning}
          />
          <Text style={styles.confidenceText}>
            {schedule?.confidence > 0.7 ? 'High confidence' : 'Weather uncertain'} -
            {schedule?.estimated_solar_kwh.toFixed(1)} kWh solar expected
          </Text>
        </View>
      </View>

      {/* Target Settings */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Charging Target</Text>

        <View style={styles.settingRow}>
          <Text style={styles.settingLabel}>Target SoC: {schedule?.target_soc || 80}%</Text>
          <Slider
            style={styles.slider}
            minimumValue={20}
            maximumValue={100}
            step={5}
            value={schedule?.target_soc || 80}
            onSlidingComplete={v => handleUpdateSchedule({ target_soc: v })}
            minimumTrackTintColor={Colors.primary}
            maximumTrackTintColor={Colors.border}
          />
        </View>

        <TouchableOpacity
          style={styles.timeButton}
          onPress={() => setShowTimePicker(true)}
        >
          <Ionicons name="time-outline" size={20} color={Colors.primary} />
          <Text style={styles.timeButtonText}>
            Departure: {config?.departure_time || 'Not set'}
          </Text>
        </TouchableOpacity>

        {/* Day selector */}
        <View style={styles.daysRow}>
          {['M', 'T', 'W', 'T', 'F', 'S', 'S'].map((day, i) => (
            <TouchableOpacity
              key={i}
              style={[
                styles.dayButton,
                config?.departure_days?.includes(i) && styles.dayButtonActive
              ]}
              onPress={() => toggleDay(i)}
            >
              <Text style={[
                styles.dayButtonText,
                config?.departure_days?.includes(i) && styles.dayButtonTextActive
              ]}>
                {day}
              </Text>
            </TouchableOpacity>
          ))}
        </View>
      </View>

      {/* Charging Strategy */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Charging Strategy</Text>

        <View style={styles.toggleRow}>
          <View>
            <Text style={styles.toggleLabel}>Prefer Solar</Text>
            <Text style={styles.toggleHint}>Wait for solar surplus when possible</Text>
          </View>
          <Switch
            value={schedule?.prefer_solar ?? true}
            onValueChange={v => handleUpdateSchedule({ prefer_solar: v })}
          />
        </View>

        <View style={styles.toggleRow}>
          <View>
            <Text style={styles.toggleLabel}>Allow Off-Peak Grid</Text>
            <Text style={styles.toggleHint}>Use cheap grid power if solar insufficient</Text>
          </View>
          <Switch
            value={schedule?.allow_grid_offpeak ?? true}
            onValueChange={v => handleUpdateSchedule({ allow_grid_offpeak: v })}
          />
        </View>

        <View style={styles.toggleRow}>
          <View>
            <Text style={styles.toggleLabel}>Allow Peak Grid</Text>
            <Text style={styles.toggleHint}>Last resort to meet target</Text>
          </View>
          <Switch
            value={schedule?.allow_grid_peak ?? false}
            onValueChange={v => handleUpdateSchedule({ allow_grid_peak: v })}
          />
        </View>
      </View>

      {/* Planned Windows */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Charging Plan</Text>

        {schedule?.planned_windows.map((window, i) => (
          <View key={i} style={styles.windowCard}>
            <View style={styles.windowHeader}>
              <Ionicons
                name={window.source === 'solar_surplus' ? 'sunny' : 'flash'}
                size={20}
                color={window.source === 'solar_surplus' ? Colors.solar : Colors.grid}
              />
              <Text style={styles.windowTime}>
                {formatTime(window.start_time)} - {formatTime(window.end_time)}
              </Text>
            </View>
            <View style={styles.windowDetails}>
              <Text style={styles.windowDetail}>
                ~{window.estimated_energy_kwh.toFixed(1)} kWh @ {window.estimated_power_kw.toFixed(1)} kW
              </Text>
              <Text style={styles.windowCost}>
                {window.source === 'solar_surplus' ? 'Free' : `$${(window.price_cents_kwh * window.estimated_energy_kwh / 100).toFixed(2)}`}
              </Text>
            </View>
          </View>
        ))}

        {/* Summary */}
        <View style={styles.summaryCard}>
          <View style={styles.summaryRow}>
            <Text style={styles.summaryLabel}>Solar</Text>
            <Text style={styles.summaryValue}>{schedule?.estimated_solar_kwh.toFixed(1)} kWh</Text>
          </View>
          <View style={styles.summaryRow}>
            <Text style={styles.summaryLabel}>Grid</Text>
            <Text style={styles.summaryValue}>{schedule?.estimated_grid_kwh.toFixed(1)} kWh</Text>
          </View>
          <View style={styles.summaryRow}>
            <Text style={styles.summaryLabel}>Est. Cost</Text>
            <Text style={styles.summaryValue}>${(schedule?.estimated_cost_cents / 100).toFixed(2)}</Text>
          </View>
        </View>
      </View>

      {/* Boost Button */}
      <TouchableOpacity style={styles.boostButton} onPress={handleBoostCharge}>
        <Ionicons name="flash" size={24} color={Colors.text} />
        <Text style={styles.boostButtonText}>Boost Charge Now</Text>
      </TouchableOpacity>
    </ScrollView>
  );
}
```

---

## Phase 3: Price-Aware Charging

### 3.1 Backend - Price Integration

**File:** `custom_components/power_sync/automations/ev_charging_planner.py`

```python
class PriceAwareCharging:
    """
    Makes charging decisions based on electricity prices.
    Integrates with Amber, Flow Power, or manual TOU rates.
    """

    def __init__(self, hass, config_entry):
        self.hass = hass
        self.config_entry = config_entry

    async def get_current_prices(self) -> dict:
        """
        Get current import and export prices.
        Returns: {"import_cents": 28.5, "export_cents": 8.0, "period": "peak"}
        """
        # Check configured provider
        provider = self.config_entry.options.get("electricity_provider", "amber")

        if provider == "amber":
            return await self._get_amber_prices()
        elif provider == "flow_power":
            return await self._get_flow_power_prices()
        else:
            return await self._get_tou_prices()

    async def get_price_forecast(self, hours: int = 24) -> List[dict]:
        """Get hourly price forecast."""
        pass

    def should_charge_at_price(
        self,
        import_price_cents: float,
        export_price_cents: float,
        config: VehicleChargingConfig,
        has_surplus: bool
    ) -> Tuple[bool, str]:
        """
        Decide if charging makes economic sense.

        Logic:
        1. If surplus available and solar_enabled: charge (free)
        2. If no surplus but export > import: charge (arbitrage)
        3. If import < max_price and offpeak_allowed: charge
        4. If deadline approaching and peak_allowed: charge
        """
        # Free solar surplus
        if has_surplus and config.solar_charging_enabled:
            return True, "solar_surplus"

        # Check if we should export instead
        if config.prefer_export_over_ev and export_price_cents > import_price_cents:
            return False, "export_more_valuable"

        # Check price threshold
        if config.max_price_cents_kwh and import_price_cents > config.max_price_cents_kwh:
            return False, "price_too_high"

        # Determine if offpeak
        is_offpeak = import_price_cents < 20  # Simplified check
        if is_offpeak and config.allow_grid_offpeak:
            return True, "offpeak_rate"

        return False, "waiting_for_better_rate"

    async def calculate_session_cost(
        self,
        energy_kwh: float,
        start_time: datetime,
        end_time: datetime
    ) -> dict:
        """
        Calculate cost/savings for a charging session.
        Accounts for varying prices during session.
        """
        pass
```

### 3.2 Mobile App - Price Display

**File:** `PowerSyncMobile/src/components/PriceIndicator.tsx` (new)

```typescript
/**
 * PriceIndicator - Shows current electricity price with charging recommendation
 */

interface Props {
  importPrice: number;  // cents/kWh
  exportPrice: number;
  surplus: number;      // kW
  recommendation: 'charge' | 'wait' | 'export';
  reason: string;
}

export function PriceIndicator({ importPrice, exportPrice, surplus, recommendation, reason }: Props) {
  const getColor = () => {
    if (recommendation === 'charge') return Colors.success;
    if (recommendation === 'wait') return Colors.warning;
    return Colors.error;
  };

  const getIcon = () => {
    if (recommendation === 'charge') return 'checkmark-circle';
    if (recommendation === 'wait') return 'time';
    return 'close-circle';
  };

  return (
    <View style={styles.container}>
      <View style={styles.priceRow}>
        <View style={styles.priceItem}>
          <Ionicons name="arrow-down" size={16} color={Colors.grid} />
          <Text style={styles.priceLabel}>Import</Text>
          <Text style={styles.priceValue}>{importPrice.toFixed(1)}c</Text>
        </View>
        <View style={styles.priceItem}>
          <Ionicons name="arrow-up" size={16} color={Colors.solar} />
          <Text style={styles.priceLabel}>Export</Text>
          <Text style={styles.priceValue}>{exportPrice.toFixed(1)}c</Text>
        </View>
        <View style={styles.priceItem}>
          <Ionicons name="sunny" size={16} color={Colors.solar} />
          <Text style={styles.priceLabel}>Surplus</Text>
          <Text style={styles.priceValue}>{surplus.toFixed(1)}kW</Text>
        </View>
      </View>

      <View style={[styles.recommendation, { backgroundColor: getColor() + '20' }]}>
        <Ionicons name={getIcon()} size={20} color={getColor()} />
        <Text style={[styles.recommendationText, { color: getColor() }]}>
          {recommendation === 'charge' ? 'Good time to charge' :
           recommendation === 'wait' ? 'Waiting for better rate' :
           'Better to export'}
        </Text>
      </View>

      <Text style={styles.reason}>{reason}</Text>
    </View>
  );
}
```

---

## Phase 4: Analytics & History

### 4.1 Backend - Statistics Calculation

**File:** `custom_components/power_sync/automations/ev_charging_session.py`

```python
class ChargingAnalytics:
    """Calculates charging statistics and insights."""

    async def get_statistics(
        self,
        vehicle_id: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> dict:
        """Calculate comprehensive statistics."""
        sessions = await self.get_sessions(vehicle_id, start_date, end_date)

        total_energy = sum(s.total_energy_kwh for s in sessions)
        solar_energy = sum(s.solar_energy_kwh for s in sessions)
        grid_energy = sum(s.grid_energy_kwh for s in sessions)

        total_cost = sum(s.total_cost_cents for s in sessions)
        savings = sum(s.grid_cost_avoided_cents for s in sessions)

        return {
            "period": {
                "start": start_date.isoformat() if start_date else None,
                "end": end_date.isoformat() if end_date else None,
                "days": (end_date - start_date).days if start_date and end_date else 30,
            },
            "sessions": {
                "count": len(sessions),
                "avg_duration_minutes": statistics.mean(s.duration_minutes for s in sessions) if sessions else 0,
                "avg_energy_kwh": statistics.mean(s.total_energy_kwh for s in sessions) if sessions else 0,
            },
            "energy": {
                "total_kwh": total_energy,
                "solar_kwh": solar_energy,
                "grid_kwh": grid_energy,
                "solar_percentage": (solar_energy / total_energy * 100) if total_energy > 0 else 0,
            },
            "cost": {
                "total_dollars": total_cost / 100,
                "savings_dollars": savings / 100,
                "avg_cost_per_kwh_cents": (total_cost / total_energy) if total_energy > 0 else 0,
                "equivalent_petrol_dollars": self._calculate_petrol_equivalent(total_energy),
            },
            "environmental": {
                "co2_avoided_kg": solar_energy * 0.5,  # ~0.5kg CO2 per kWh from grid
                "trees_equivalent": solar_energy * 0.5 / 21,  # ~21kg CO2 per tree per year
            },
            "trends": {
                "daily": self._calculate_daily_trends(sessions),
                "weekly": self._calculate_weekly_trends(sessions),
                "solar_trend": self._calculate_solar_trend(sessions),
            },
        }

    def _calculate_petrol_equivalent(self, energy_kwh: float) -> float:
        """Calculate equivalent petrol cost for same distance."""
        # Assume 6km/kWh for EV, 12km/L for petrol, $2/L petrol
        ev_km = energy_kwh * 6
        petrol_litres = ev_km / 12
        return petrol_litres * 2.0
```

### 4.2 Mobile App - Statistics Screen

**File:** `PowerSyncMobile/src/screens/EVStatisticsScreen.tsx` (new)

```typescript
/**
 * EVStatisticsScreen - Charging analytics and history
 */

export default function EVStatisticsScreen() {
  const [stats, setStats] = useState<ChargingStatistics | null>(null);
  const [sessions, setSessions] = useState<ChargingSession[]>([]);
  const [period, setPeriod] = useState<'week' | 'month' | 'year'>('month');

  return (
    <ScrollView style={styles.container}>
      {/* Period Selector */}
      <View style={styles.periodSelector}>
        {(['week', 'month', 'year'] as const).map(p => (
          <TouchableOpacity
            key={p}
            style={[styles.periodButton, period === p && styles.periodButtonActive]}
            onPress={() => setPeriod(p)}
          >
            <Text style={[styles.periodText, period === p && styles.periodTextActive]}>
              {p.charAt(0).toUpperCase() + p.slice(1)}
            </Text>
          </TouchableOpacity>
        ))}
      </View>

      {/* Summary Cards */}
      <View style={styles.summaryGrid}>
        <StatCard
          icon="flash"
          label="Total Energy"
          value={`${stats?.energy.total_kwh.toFixed(0)} kWh`}
          color={Colors.primary}
        />
        <StatCard
          icon="sunny"
          label="Solar"
          value={`${stats?.energy.solar_percentage.toFixed(0)}%`}
          subValue={`${stats?.energy.solar_kwh.toFixed(0)} kWh`}
          color={Colors.solar}
        />
        <StatCard
          icon="wallet"
          label="Total Cost"
          value={`$${stats?.cost.total_dollars.toFixed(2)}`}
          color={Colors.error}
        />
        <StatCard
          icon="trending-down"
          label="Savings"
          value={`$${stats?.cost.savings_dollars.toFixed(2)}`}
          subValue="vs grid charging"
          color={Colors.success}
        />
      </View>

      {/* Energy Chart */}
      <View style={styles.chartSection}>
        <Text style={styles.sectionTitle}>Energy by Day</Text>
        <EnergyChart data={stats?.trends.daily || []} />
      </View>

      {/* Solar vs Grid Breakdown */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Energy Sources</Text>
        <View style={styles.breakdownBar}>
          <View
            style={[
              styles.breakdownSegment,
              { flex: stats?.energy.solar_percentage || 0, backgroundColor: Colors.solar }
            ]}
          />
          <View
            style={[
              styles.breakdownSegment,
              { flex: 100 - (stats?.energy.solar_percentage || 0), backgroundColor: Colors.grid }
            ]}
          />
        </View>
        <View style={styles.breakdownLegend}>
          <View style={styles.legendItem}>
            <View style={[styles.legendDot, { backgroundColor: Colors.solar }]} />
            <Text style={styles.legendText}>Solar ({stats?.energy.solar_kwh.toFixed(0)} kWh)</Text>
          </View>
          <View style={styles.legendItem}>
            <View style={[styles.legendDot, { backgroundColor: Colors.grid }]} />
            <Text style={styles.legendText}>Grid ({stats?.energy.grid_kwh.toFixed(0)} kWh)</Text>
          </View>
        </View>
      </View>

      {/* Environmental Impact */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Environmental Impact</Text>
        <View style={styles.impactRow}>
          <View style={styles.impactItem}>
            <Ionicons name="leaf" size={32} color={Colors.success} />
            <Text style={styles.impactValue}>{stats?.environmental.co2_avoided_kg.toFixed(0)} kg</Text>
            <Text style={styles.impactLabel}>CO2 Avoided</Text>
          </View>
          <View style={styles.impactItem}>
            <Ionicons name="car" size={32} color={Colors.primary} />
            <Text style={styles.impactValue}>${stats?.cost.equivalent_petrol_dollars.toFixed(0)}</Text>
            <Text style={styles.impactLabel}>vs Petrol</Text>
          </View>
        </View>
      </View>

      {/* Recent Sessions */}
      <View style={styles.section}>
        <Text style={styles.sectionTitle}>Recent Sessions</Text>
        {sessions.slice(0, 5).map(session => (
          <SessionCard key={session.id} session={session} />
        ))}
        <TouchableOpacity style={styles.viewAllButton}>
          <Text style={styles.viewAllText}>View All Sessions</Text>
        </TouchableOpacity>
      </View>
    </ScrollView>
  );
}

function SessionCard({ session }: { session: ChargingSession }) {
  return (
    <View style={styles.sessionCard}>
      <View style={styles.sessionHeader}>
        <Text style={styles.sessionDate}>
          {formatDate(session.start_time)}
        </Text>
        <View style={[
          styles.sessionBadge,
          { backgroundColor: session.solar_percentage > 80 ? Colors.solar + '20' : Colors.grid + '20' }
        ]}>
          <Text style={styles.sessionBadgeText}>
            {session.solar_percentage.toFixed(0)}% Solar
          </Text>
        </View>
      </View>

      <View style={styles.sessionDetails}>
        <View style={styles.sessionDetail}>
          <Text style={styles.sessionDetailLabel}>Energy</Text>
          <Text style={styles.sessionDetailValue}>{session.total_energy_kwh.toFixed(1)} kWh</Text>
        </View>
        <View style={styles.sessionDetail}>
          <Text style={styles.sessionDetailLabel}>Duration</Text>
          <Text style={styles.sessionDetailValue}>{formatDuration(session.start_time, session.end_time)}</Text>
        </View>
        <View style={styles.sessionDetail}>
          <Text style={styles.sessionDetailLabel}>Cost</Text>
          <Text style={styles.sessionDetailValue}>${(session.total_cost_cents / 100).toFixed(2)}</Text>
        </View>
      </View>

      {/* SoC progress */}
      {session.start_soc && session.end_soc && (
        <View style={styles.socProgress}>
          <Text style={styles.socText}>{session.start_soc}%</Text>
          <View style={styles.socBar}>
            <View style={[styles.socFill, { width: `${session.end_soc}%` }]} />
          </View>
          <Text style={styles.socText}>{session.end_soc}%</Text>
        </View>
      )}
    </View>
  );
}
```

---

## Phase 5: Notifications & Widgets

### 5.1 Backend - Push Notifications

**File:** `custom_components/power_sync/automations/ev_notifications.py` (new)

```python
class EVChargingNotifications:
    """Sends push notifications for EV charging events."""

    NOTIFICATION_TYPES = {
        "charging_started": {
            "title": "EV Charging Started",
            "body": "{vehicle} started charging at {power}kW ({source})",
            "data": {"screen": "ev_charging"}
        },
        "charging_completed": {
            "title": "EV Charging Complete",
            "body": "{vehicle} charged {energy}kWh ({solar_pct}% solar) - ${cost}",
            "data": {"screen": "ev_charging"}
        },
        "target_reached": {
            "title": "EV Target Reached",
            "body": "{vehicle} reached {soc}% - ready for departure",
            "data": {"screen": "ev_charging"}
        },
        "charging_paused": {
            "title": "EV Charging Paused",
            "body": "{vehicle} paused: {reason}",
            "data": {"screen": "ev_charging"}
        },
        "surplus_available": {
            "title": "Solar Surplus Available",
            "body": "{surplus}kW surplus - {vehicle} can charge",
            "data": {"screen": "ev_charging", "action": "start_surplus"}
        },
        "departure_warning": {
            "title": "Departure Warning",
            "body": "{vehicle} at {soc}% - may not reach {target}% by {time}",
            "data": {"screen": "ev_schedule", "action": "boost"}
        },
    }

    async def notify(self, notification_type: str, vehicle_id: str, data: dict):
        """Send notification if enabled for this vehicle."""
        config = await self.get_vehicle_config(vehicle_id)

        # Check notification preferences
        if notification_type.startswith("charging_") and not config.notify_on_start:
            return
        if notification_type == "charging_completed" and not config.notify_on_complete:
            return

        template = self.NOTIFICATION_TYPES.get(notification_type)
        if not template:
            return

        title = template["title"]
        body = template["body"].format(**data)

        await self._send_expo_push(title, body, template.get("data", {}))
```

### 5.2 Mobile App - Home Screen Widget

**File:** `PowerSyncMobile/src/widgets/EVChargingWidget.tsx` (new)

```typescript
/**
 * EVChargingWidget - Home screen widget showing charging status
 *
 * For iOS: Uses WidgetKit via react-native-widget-extension
 * For Android: Uses Glance via @react-native-widgets/glance
 */

// Widget data interface (shared with native code)
export interface EVWidgetData {
  vehicle_name: string;
  is_charging: boolean;
  current_soc: number;
  target_soc: number;
  current_power_kw: number;
  source: 'solar' | 'grid' | 'idle';
  eta_minutes?: number;
  surplus_kw: number;
}

// Widget component (rendered natively)
export function EVChargingWidgetView({ data }: { data: EVWidgetData }) {
  return (
    <View style={styles.widget}>
      {/* Top row: Vehicle name + charging indicator */}
      <View style={styles.topRow}>
        <Text style={styles.vehicleName}>{data.vehicle_name}</Text>
        {data.is_charging && (
          <View style={[
            styles.chargingBadge,
            { backgroundColor: data.source === 'solar' ? Colors.solar : Colors.grid }
          ]}>
            <Ionicons
              name={data.source === 'solar' ? 'sunny' : 'flash'}
              size={12}
              color="white"
            />
          </View>
        )}
      </View>

      {/* SoC display */}
      <View style={styles.socContainer}>
        <Text style={styles.socValue}>{data.current_soc}%</Text>
        <View style={styles.socBar}>
          <View
            style={[
              styles.socFill,
              { width: `${data.current_soc}%` },
              { backgroundColor: data.source === 'solar' ? Colors.solar : Colors.primary }
            ]}
          />
          {data.target_soc && (
            <View style={[styles.targetMarker, { left: `${data.target_soc}%` }]} />
          )}
        </View>
      </View>

      {/* Bottom row: Power or surplus info */}
      <View style={styles.bottomRow}>
        {data.is_charging ? (
          <>
            <Text style={styles.powerText}>{data.current_power_kw.toFixed(1)} kW</Text>
            {data.eta_minutes && (
              <Text style={styles.etaText}>~{Math.round(data.eta_minutes / 60)}h to {data.target_soc}%</Text>
            )}
          </>
        ) : (
          <Text style={styles.surplusText}>
            {data.surplus_kw > 0.5
              ? `${data.surplus_kw.toFixed(1)} kW surplus available`
              : 'Waiting for surplus'
            }
          </Text>
        )}
      </View>
    </View>
  );
}
```

---

## Phase 6: Navigation & Screen Integration

### 6.1 Mobile App - Navigation Structure

**File:** `PowerSyncMobile/src/navigation/EVNavigator.tsx` (new)

```typescript
import { createStackNavigator } from '@react-navigation/stack';

export type EVStackParamList = {
  EVChargingHome: undefined;
  EVSchedule: { vehicleId: string };
  EVStatistics: { vehicleId?: string };
  EVSessionDetail: { sessionId: string };
  EVVehicleSettings: { vehicleId: string };
};

const Stack = createStackNavigator<EVStackParamList>();

export function EVNavigator() {
  return (
    <Stack.Navigator
      screenOptions={{
        headerStyle: { backgroundColor: Colors.surface },
        headerTintColor: Colors.text,
      }}
    >
      <Stack.Screen
        name="EVChargingHome"
        component={EVChargingScreen}
        options={{ title: 'EV Charging' }}
      />
      <Stack.Screen
        name="EVSchedule"
        component={EVScheduleScreen}
        options={{ title: 'Charging Schedule' }}
      />
      <Stack.Screen
        name="EVStatistics"
        component={EVStatisticsScreen}
        options={{ title: 'Charging Stats' }}
      />
      <Stack.Screen
        name="EVSessionDetail"
        component={EVSessionDetailScreen}
        options={{ title: 'Session Details' }}
      />
      <Stack.Screen
        name="EVVehicleSettings"
        component={EVVehicleSettingsScreen}
        options={{ title: 'Vehicle Settings' }}
      />
    </Stack.Navigator>
  );
}
```

### 6.2 Mobile App - Updated EV Charging Screen

**File:** `PowerSyncMobile/src/screens/EVChargingScreen.tsx` (updates)

```typescript
// Add navigation to new screens

export default function EVChargingScreen() {
  const navigation = useNavigation<EVNavigationProp>();

  // ... existing code ...

  return (
    <ScrollView>
      {/* Quick Actions Bar */}
      <View style={styles.quickActions}>
        <TouchableOpacity
          style={styles.quickAction}
          onPress={() => navigation.navigate('EVSchedule', { vehicleId: selectedVehicle })}
        >
          <Ionicons name="calendar" size={24} color={Colors.primary} />
          <Text style={styles.quickActionText}>Schedule</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={styles.quickAction}
          onPress={() => navigation.navigate('EVStatistics')}
        >
          <Ionicons name="stats-chart" size={24} color={Colors.primary} />
          <Text style={styles.quickActionText}>Stats</Text>
        </TouchableOpacity>

        <TouchableOpacity
          style={styles.quickAction}
          onPress={handleBoostCharge}
        >
          <Ionicons name="flash" size={24} color={Colors.warning} />
          <Text style={styles.quickActionText}>Boost</Text>
        </TouchableOpacity>
      </View>

      {/* ... rest of existing UI ... */}

      {/* Vehicle cards with settings button */}
      {vehicles.map(vehicle => (
        <VehicleCard
          key={vehicle.id}
          vehicle={vehicle}
          onCommand={handleVehicleCommand}
          onSettings={() => navigation.navigate('EVVehicleSettings', { vehicleId: vehicle.id })}
          onSchedule={() => navigation.navigate('EVSchedule', { vehicleId: vehicle.id })}
        />
      ))}
    </ScrollView>
  );
}
```

---

## Phase 7: Advanced Features

### 7.1 Load Balancing

**File:** `custom_components/power_sync/automations/load_balancer.py` (new)

```python
class HomeLoadBalancer:
    """
    Coordinates EV charging with other high-draw appliances.
    Prevents circuit overload and optimizes for solar self-consumption.
    """

    def __init__(self, hass):
        self.hass = hass
        self.max_home_load_kw = 10.0  # Configurable
        self.monitored_loads = []  # List of entity_ids to monitor

    async def get_current_loads(self) -> dict:
        """Get current power draw of monitored loads."""
        loads = {}
        for entity_id in self.monitored_loads:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ('unavailable', 'unknown'):
                loads[entity_id] = float(state.state)
        return loads

    async def get_available_capacity(self, exclude_ev: bool = True) -> float:
        """Calculate available capacity for EV charging."""
        current_loads = await self.get_current_loads()
        total_load = sum(current_loads.values())

        # Get current EV load if excluding
        ev_load = 0
        if exclude_ev:
            ev_state = self.hass.states.get('sensor.ev_charger_power')
            if ev_state:
                ev_load = float(ev_state.state)

        non_ev_load = total_load - ev_load
        available = self.max_home_load_kw - non_ev_load

        return max(0, available)

    async def should_reduce_ev_power(self) -> Tuple[bool, float]:
        """
        Check if EV charging should be reduced due to other loads.
        Returns: (should_reduce, new_max_kw)
        """
        available = await self.get_available_capacity(exclude_ev=False)
        if available < 0:
            # Over capacity, need to reduce EV
            return True, max(0, available + 2.0)  # Reduce to fit
        return False, self.max_home_load_kw
```

### 7.2 Three-Phase Support

**File:** `custom_components/power_sync/automations/actions.py` (updates)

```python
class ThreePhaseCharger:
    """Handles three-phase EV charger control."""

    def __init__(self, hass, config: VehicleChargingConfig):
        self.hass = hass
        self.config = config
        self.phase_entities = {
            'L1': config.charger_amps_entity_l1,
            'L2': config.charger_amps_entity_l2,
            'L3': config.charger_amps_entity_l3,
        }

    async def set_balanced_amps(self, total_amps: int):
        """Set equal amps on all phases."""
        per_phase = total_amps // 3
        for phase, entity in self.phase_entities.items():
            if entity:
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": entity, "value": per_phase}
                )

    async def set_unbalanced_amps(self, phase_amps: dict):
        """Set different amps per phase (for phase balancing)."""
        for phase, amps in phase_amps.items():
            entity = self.phase_entities.get(phase)
            if entity:
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": entity, "value": amps}
                )

    def calculate_power_kw(self, amps: int) -> float:
        """Calculate three-phase power."""
        return (amps * self.config.voltage * 1.732) / 1000  # sqrt(3) for 3-phase
```

---

## Implementation Order

### Sprint 1: Data Foundation (Week 1-2)
1. Charging session tracking backend
2. Session storage and persistence
3. Basic API endpoints
4. Mobile types and API methods

### Sprint 2: Statistics & History (Week 3-4)
1. Statistics calculation
2. EVStatisticsScreen
3. Session detail screen
4. Cost/savings calculations

### Sprint 3: Smart Scheduling (Week 5-6)
1. Solcast forecast integration
2. Charging planner
3. EVScheduleScreen
4. Target time feature

### Sprint 4: Price Awareness (Week 7-8)
1. Price forecast integration
2. Price-aware charging decisions
3. Price indicators in UI
4. Export vs charge logic

### Sprint 5: Notifications & Polish (Week 9-10)
1. Push notification system
2. Widget implementation
3. Navigation refinement
4. Testing and bug fixes

### Sprint 6: Advanced Features (Week 11-12)
1. Load balancing
2. Three-phase support
3. Calendar integration
4. Performance optimization

---

## Files to Create/Modify

### Backend (power-sync)

| File | Type | Description |
|------|------|-------------|
| `automations/ev_charging_session.py` | New | Session tracking and management |
| `automations/ev_charging_planner.py` | New | Forecast-based planning |
| `automations/ev_notifications.py` | New | Push notification triggers |
| `automations/load_balancer.py` | New | Home load coordination |
| `automations/actions.py` | Modify | Extended vehicle config |
| `__init__.py` | Modify | New API endpoints |

### Mobile App (PowerSyncMobile)

| File | Type | Description |
|------|------|-------------|
| `types/evCharging.ts` | New | Extended TypeScript types |
| `screens/EVScheduleScreen.tsx` | New | Scheduling UI |
| `screens/EVStatisticsScreen.tsx` | New | Analytics UI |
| `screens/EVSessionDetailScreen.tsx` | New | Session detail view |
| `screens/EVVehicleSettingsScreen.tsx` | New | Per-vehicle settings |
| `components/PriceIndicator.tsx` | New | Price display component |
| `components/EnergyChart.tsx` | New | Chart for statistics |
| `navigation/EVNavigator.tsx` | New | EV screen navigation |
| `widgets/EVChargingWidget.tsx` | New | Home screen widget |
| `screens/EVChargingScreen.tsx` | Modify | Add navigation, quick actions |
| `services/backends/haBackend.ts` | Modify | New API methods |

---

## Testing Plan

### Unit Tests
- Surplus calculation accuracy
- Price comparison logic
- Schedule planning algorithm
- Statistics calculations

### Integration Tests
- API endpoint responses
- Session tracking persistence
- Notification delivery
- Widget data updates

### Manual Testing
- Full charging cycle with session tracking
- Schedule creation and execution
- Price-based charging decisions
- Multi-vehicle coordination
- Widget refresh and accuracy

---

## Success Metrics

1. **Solar Utilization**: >80% of EV charging from solar when surplus available
2. **Cost Savings**: Track and display savings vs grid-only charging
3. **Reliability**: <1% failed charging sessions due to system issues
4. **User Engagement**: Daily active widget views, schedule usage
5. **Performance**: <2s API response times, <100ms UI interactions
