"""Data update coordinators for PowerSync with improved error handling."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, date
import logging
import re
import time
from typing import Any, Optional
import asyncio

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

# Dispatcher signal fired by AEMOPriceCoordinator when a new dispatch file is
# detected on NEMWEB (settled price for the period that just ended). TOU sync
# subscribes to this in __init__.py to issue exactly one tariff POST per
# 5-min period, aligned with AEMO's publish event instead of a fixed cron.
SIGNAL_AEMO_NEW_DISPATCH = "power_sync_aemo_new_dispatch"

from .const import (
    DOMAIN,
    UPDATE_INTERVAL_PRICES,
    UPDATE_INTERVAL_ENERGY,
    AMBER_API_BASE_URL,
    TESLEMETRY_API_BASE_URL,
    FLEET_API_BASE_URL,
    POWERSYNC_API_BASE_URL,
    TESLA_PROVIDER_TESLEMETRY,
    TESLA_PROVIDER_FLEET_API,
    TESLA_PROVIDER_POWERSYNC,
    POWER_SYNC_USER_AGENT,
    DEFAULT_SOLCAST_ESTIMATE_TYPE,
    SOLCAST_ESTIMATE,
    SOLCAST_ESTIMATE10,
    SOLCAST_ESTIMATE90,
    DEFAULT_TWAP_WINDOW_DAYS,
    MIN_TWAP_SAMPLES,
    FLOW_POWER_MARKET_AVG,
    FLOW_POWER_KWATCH_REGIONS,
    CONF_FLEET_API_BASE_URL,
    TESLA_SITE_INFO_CACHE_TTL_SECONDS,
    CONF_SIGENERGY_CHARGER_ENABLED,
    CONF_SIGENERGY_CHARGER_TYPE,
    SIGENERGY_CHARGER_EVAC,
    SIGENERGY_CHARGER_EVDC,
)
from .sensitive_logging import obfuscate_vin_tokens
from .sigenergy_model import sigenergy_home_load_kw

_SOLCAST_ESTIMATE_FIELDS = {
    SOLCAST_ESTIMATE: ("pv_estimate", "pv_estimate50"),
    SOLCAST_ESTIMATE10: ("pv_estimate10", "pv_estimate", "pv_estimate50"),
    SOLCAST_ESTIMATE90: ("pv_estimate90", "pv_estimate", "pv_estimate50"),
}


ENERGY_ACC_STORE_VERSION = 1
ENERGY_ACC_SAVE_DELAY = 300  # Flush at most every 5 minutes
SOLAREDGE_DAILY_TOTALS_STORE_VERSION = 1
LIFETIME_TOTALS_STORE_VERSION = 1
TESLA_OUTAGE_NOTIFY_FAILURES = 5
TESLA_OUTAGE_NOTIFY_MIN_SECONDS = 300
LIFETIME_TOTAL_KEYS = (
    "lifetime_solar_kwh",
    "lifetime_grid_import_kwh",
    "lifetime_grid_export_kwh",
    "lifetime_battery_charged_kwh",
    "lifetime_battery_discharged_kwh",
    "lifetime_home_kwh",
)


def _configured_ac_inverter_power_kw(hass: HomeAssistant, entry_id: str) -> float:
    """Return the latest separately configured AC inverter output in kW."""
    attrs = (
        hass.data.get(DOMAIN, {})
        .get(entry_id, {})
        .get("inverter_attributes")
        or {}
    )
    power_w = attrs.get("power_output_w")
    if power_w is None:
        power_w = attrs.get("dc_power")
    try:
        return max(0.0, float(power_w or 0) / 1000.0)
    except (TypeError, ValueError):
        return 0.0


def _stored_battery_health_capacity_kwh(hass: HomeAssistant, entry_id: str) -> float | None:
    """Return the latest BMS-scanned current Powerwall capacity in kWh."""
    health = (
        hass.data.get(DOMAIN, {})
        .get(entry_id, {})
        .get("battery_health")
        or {}
    )
    capacity_wh = health.get("current_capacity_wh")
    try:
        capacity_kwh = float(capacity_wh) / 1000.0
    except (TypeError, ValueError):
        return None
    return round(capacity_kwh, 2) if capacity_kwh > 0 else None


class EnergyAccumulator:
    """Accumulates daily energy totals from instantaneous power readings.

    Integrates power (kW) over time to estimate daily energy (kWh).
    Resets at local midnight. Persisted via HA Store to survive restarts.
    """

    def __init__(self, hass: HomeAssistant | None = None, store_key: str = "") -> None:
        self._hass = hass
        self._last_update: datetime | None = None
        self._last_date: Any = None
        self.solar_kwh: float = 0.0
        self.grid_import_kwh: float = 0.0
        self.grid_export_kwh: float = 0.0
        self.battery_charge_kwh: float = 0.0
        self.battery_discharge_kwh: float = 0.0
        self.load_kwh: float = 0.0
        self.import_cost_today: float = 0.0
        self.export_earnings_today: float = 0.0
        self.mtd_solar_kwh: float = 0.0
        self.mtd_grid_import_kwh: float = 0.0
        self.mtd_grid_export_kwh: float = 0.0
        self.mtd_battery_charge_kwh: float = 0.0
        self.mtd_battery_discharge_kwh: float = 0.0
        self.mtd_load_kwh: float = 0.0
        self.mtd_import_cost: float = 0.0
        self.mtd_export_earnings: float = 0.0
        self._last_month: Any = None
        self._store: Store | None = None
        if hass and store_key:
            self._store = Store(
                hass,
                ENERGY_ACC_STORE_VERSION,
                f"power_sync.energy_acc.{store_key}",
            )

    async def async_restore(self) -> None:
        """Restore accumulated energy from persistent storage."""
        if not self._store:
            return
        try:
            data = await self._store.async_load()
        except Exception as e:
            _LOGGER.warning("Failed to load persisted energy accumulator: %s", e)
            return
        if not data:
            return
        stored_date = data.get("date")
        today = dt_util.now().strftime("%Y-%m-%d")
        if stored_date == today:
            self.solar_kwh = float(data.get("solar_kwh", 0.0))
            self.grid_import_kwh = float(data.get("grid_import_kwh", 0.0))
            self.grid_export_kwh = float(data.get("grid_export_kwh", 0.0))
            self.battery_charge_kwh = float(data.get("battery_charge_kwh", 0.0))
            self.battery_discharge_kwh = float(data.get("battery_discharge_kwh", 0.0))
            self.load_kwh = float(data.get("load_kwh", 0.0))
            self.import_cost_today = float(data.get("import_cost_today", 0.0))
            self.export_earnings_today = float(data.get("export_earnings_today", 0.0))
            _LOGGER.info(
                "Restored energy accumulator: solar=%.2f grid_in=%.2f grid_out=%.2f "
                "charge=%.2f discharge=%.2f load=%.2f kWh, cost=$%.2f earn=$%.2f (date=%s)",
                self.solar_kwh, self.grid_import_kwh, self.grid_export_kwh,
                self.battery_charge_kwh, self.battery_discharge_kwh, self.load_kwh,
                self.import_cost_today, self.export_earnings_today,
                stored_date,
            )
        else:
            _LOGGER.debug(
                "Energy accumulator data from %s (today=%s), starting fresh",
                stored_date, today,
            )
        stored_month = data.get("month")
        current_month = dt_util.now().strftime("%Y-%m")
        if stored_month == current_month:
            self.mtd_solar_kwh = float(data.get("mtd_solar_kwh", 0.0))
            self.mtd_grid_import_kwh = float(data.get("mtd_grid_import_kwh", 0.0))
            self.mtd_grid_export_kwh = float(data.get("mtd_grid_export_kwh", 0.0))
            self.mtd_battery_charge_kwh = float(data.get("mtd_battery_charge_kwh", 0.0))
            self.mtd_battery_discharge_kwh = float(data.get("mtd_battery_discharge_kwh", 0.0))
            self.mtd_load_kwh = float(data.get("mtd_load_kwh", 0.0))
            self.mtd_import_cost = float(data.get("mtd_import_cost", 0.0))
            self.mtd_export_earnings = float(data.get("mtd_export_earnings", 0.0))

    async def async_flush(self) -> None:
        """Immediately write current energy data to persistent storage.

        Called during integration unload so the next restore gets the latest
        values, preventing total_increasing sensors from going backwards.
        """
        if not self._store:
            return
        await self._store.async_save(self._data_to_save())

    def _schedule_save(self) -> None:
        """Schedule a coalesced write of energy data to persistent storage."""
        if not self._store:
            return
        self._store.async_delay_save(
            self._data_to_save,
            ENERGY_ACC_SAVE_DELAY,
        )

    def _data_to_save(self) -> dict:
        """Return energy data dict for Store serialization."""
        return {
            "date": dt_util.now().strftime("%Y-%m-%d"),
            "solar_kwh": round(self.solar_kwh, 4),
            "grid_import_kwh": round(self.grid_import_kwh, 4),
            "grid_export_kwh": round(self.grid_export_kwh, 4),
            "battery_charge_kwh": round(self.battery_charge_kwh, 4),
            "battery_discharge_kwh": round(self.battery_discharge_kwh, 4),
            "load_kwh": round(self.load_kwh, 4),
            "import_cost_today": round(self.import_cost_today, 4),
            "export_earnings_today": round(self.export_earnings_today, 4),
            "month": dt_util.now().strftime("%Y-%m"),
            "mtd_solar_kwh": round(self.mtd_solar_kwh, 4),
            "mtd_grid_import_kwh": round(self.mtd_grid_import_kwh, 4),
            "mtd_grid_export_kwh": round(self.mtd_grid_export_kwh, 4),
            "mtd_battery_charge_kwh": round(self.mtd_battery_charge_kwh, 4),
            "mtd_battery_discharge_kwh": round(self.mtd_battery_discharge_kwh, 4),
            "mtd_load_kwh": round(self.mtd_load_kwh, 4),
            "mtd_import_cost": round(self.mtd_import_cost, 4),
            "mtd_export_earnings": round(self.mtd_export_earnings, 4),
        }

    def update(
        self,
        solar_kw: float,
        grid_kw: float,
        battery_kw: float,
        load_kw: float,
        buy_price_per_kwh: float | None = None,
        sell_price_per_kwh: float | None = None,
    ) -> None:
        """Update accumulators with current power readings.

        Sign conventions (standard PowerSync format):
            solar_kw: always >= 0
            grid_kw: positive = importing, negative = exporting
            battery_kw: positive = discharging, negative = charging
            load_kw: always >= 0

        Optional cost tracking:
            buy_price_per_kwh: current import price in $/kWh (None = skip cost tracking)
            sell_price_per_kwh: current export/feed-in price in $/kWh (None = skip cost tracking)
        """
        now = dt_util.now()  # Local time for midnight reset

        # Reset MTD at month rollover
        if self._last_month is not None and now.month != self._last_month:
            self.mtd_solar_kwh = 0.0
            self.mtd_grid_import_kwh = 0.0
            self.mtd_grid_export_kwh = 0.0
            self.mtd_battery_charge_kwh = 0.0
            self.mtd_battery_discharge_kwh = 0.0
            self.mtd_load_kwh = 0.0
            self.mtd_import_cost = 0.0
            self.mtd_export_earnings = 0.0

        # Reset at local midnight
        if self._last_date is not None and now.date() != self._last_date:
            _LOGGER.info(
                "Energy accumulator midnight reset: solar=%.2f grid_in=%.2f grid_out=%.2f "
                "charge=%.2f discharge=%.2f load=%.2f kWh, cost=$%.2f earn=$%.2f",
                self.solar_kwh, self.grid_import_kwh, self.grid_export_kwh,
                self.battery_charge_kwh, self.battery_discharge_kwh, self.load_kwh,
                self.import_cost_today, self.export_earnings_today,
            )
            self.solar_kwh = 0.0
            self.grid_import_kwh = 0.0
            self.grid_export_kwh = 0.0
            self.battery_charge_kwh = 0.0
            self.battery_discharge_kwh = 0.0
            self.load_kwh = 0.0
            self.import_cost_today = 0.0
            self.export_earnings_today = 0.0

        # Integrate power × time
        if self._last_update is not None:
            delta_h = (now - self._last_update).total_seconds() / 3600
            if 0 < delta_h < 0.1:  # Sanity: skip if > 6 min gap (stale/restart)
                self.solar_kwh += max(0, solar_kw) * delta_h
                self.grid_import_kwh += max(0, grid_kw) * delta_h
                self.grid_export_kwh += max(0, -grid_kw) * delta_h
                self.battery_charge_kwh += max(0, -battery_kw) * delta_h
                self.battery_discharge_kwh += max(0, battery_kw) * delta_h
                self.load_kwh += max(0, load_kw) * delta_h
                # Accumulate costs if prices available
                if buy_price_per_kwh is not None:
                    self.import_cost_today += max(0, grid_kw) * buy_price_per_kwh * delta_h
                if sell_price_per_kwh is not None:
                    self.export_earnings_today += max(0, -grid_kw) * sell_price_per_kwh * delta_h
                # MTD accumulation
                self.mtd_solar_kwh += max(0, solar_kw) * delta_h
                self.mtd_grid_import_kwh += max(0, grid_kw) * delta_h
                self.mtd_grid_export_kwh += max(0, -grid_kw) * delta_h
                self.mtd_battery_charge_kwh += max(0, -battery_kw) * delta_h
                self.mtd_battery_discharge_kwh += max(0, battery_kw) * delta_h
                self.mtd_load_kwh += max(0, load_kw) * delta_h
                if buy_price_per_kwh is not None:
                    self.mtd_import_cost += max(0, grid_kw) * buy_price_per_kwh * delta_h
                if sell_price_per_kwh is not None:
                    self.mtd_export_earnings += max(0, -grid_kw) * sell_price_per_kwh * delta_h
                self._schedule_save()

        self._last_update = now
        self._last_date = now.date()
        self._last_month = now.month

    def as_dict(self) -> dict:
        """Return accumulated totals as a dict for energy_summary."""
        avg_today = (
            round((self.import_cost_today - self.export_earnings_today) / self.load_kwh, 4)
            if self.load_kwh > 0 else None
        )
        avg_mtd = (
            round((self.mtd_import_cost - self.mtd_export_earnings) / self.mtd_load_kwh, 4)
            if self.mtd_load_kwh > 0 else None
        )
        return {
            "pv_today_kwh": round(self.solar_kwh, 3),
            "grid_import_today_kwh": round(self.grid_import_kwh, 3),
            "grid_export_today_kwh": round(self.grid_export_kwh, 3),
            "charge_today_kwh": round(self.battery_charge_kwh, 3),
            "discharge_today_kwh": round(self.battery_discharge_kwh, 3),
            "load_today_kwh": round(self.load_kwh, 3),
            "import_cost_today": round(self.import_cost_today, 4),
            "export_earnings_today": round(self.export_earnings_today, 4),
            "avg_cost_per_kwh_today": avg_today,
            "mtd_import_cost": round(self.mtd_import_cost, 4),
            "mtd_export_earnings": round(self.mtd_export_earnings, 4),
            "mtd_load_kwh": round(self.mtd_load_kwh, 3),
            "avg_cost_per_kwh_mtd": avg_mtd,
        }


def _flow_power_export_rate_dollars(config_entry: Any, state: str) -> float:
    """Return configured Flow Power Happy Hour export rate in $/kWh."""
    from .const import CONF_FLOW_POWER_EXPORT_RATE, FLOW_POWER_EXPORT_RATES

    configured_rate = config_entry.options.get(
        CONF_FLOW_POWER_EXPORT_RATE,
        config_entry.data.get(CONF_FLOW_POWER_EXPORT_RATE),
    )
    if configured_rate not in (None, ""):
        try:
            return max(0.0, float(configured_rate) / 100)
        except (ValueError, TypeError):
            pass

    return FLOW_POWER_EXPORT_RATES.get(state, 0.0)


def _get_current_prices(hass: HomeAssistant, entry_id: str) -> tuple[float | None, float | None]:
    """Get current buy/sell prices in $/kWh for cost tracking.

    Priority: Amber coordinator → AEMO/Flow Power coordinator → tariff schedule.
    Returns (buy_price_per_kwh, sell_price_per_kwh) or (None, None) on failure.
    """
    try:
        entry_data = hass.data.get(DOMAIN, {}).get(entry_id, {})

        # Try Amber coordinator first (real-time market prices)
        amber_coordinator = entry_data.get("amber_coordinator")
        if amber_coordinator and amber_coordinator.data:
            current_prices = amber_coordinator.data.get("current", [])
            buy_cents = None
            sell_cents = None
            for price in current_prices:
                channel = price.get("channelType", "")
                if channel == "general":
                    buy_cents = price.get("perKwh")
                elif channel == "feedIn":
                    sell_cents = price.get("perKwh")
            if buy_cents is not None:
                # Amber perKwh is in cents → convert to $/kWh
                buy_dollar = buy_cents / 100.0
                sell_dollar = (sell_cents / 100.0) if sell_cents is not None else 0.0
                # Amber feedIn: negative = you earn, positive = you pay to export
                # Negate so sell_price is positive when earning, negative when paying
                return (buy_dollar, -sell_dollar)

        # Try AEMO sensor coordinator (Flow Power and AEMO users)
        aemo_coordinator = entry_data.get("aemo_sensor_coordinator")
        if aemo_coordinator and aemo_coordinator.data:
            current_prices = aemo_coordinator.data.get("current", [])
            wholesale_cents = None
            sell_cents_raw = None
            for price in current_prices:
                channel = price.get("channelType", "")
                if channel == "general":
                    wholesale_cents = price.get("perKwh")
                elif channel == "feedIn":
                    sell_cents_raw = price.get("perKwh")
            if wholesale_cents is not None:
                config_entry = hass.config_entries.async_get_entry(entry_id)
                if config_entry:
                    from .const import (
                        CONF_ELECTRICITY_PROVIDER,
                        CONF_PEA_ENABLED,
                        CONF_FLOW_POWER_BASE_RATE,
                        CONF_PEA_CUSTOM_VALUE,
                        CONF_FLOW_POWER_STATE,
                        FLOW_POWER_DEFAULT_BASE_RATE,
                        FLOW_POWER_HAPPY_HOUR_PERIODS,
                    )
                    from .flow_power_pricing import (
                        calculate_flow_power_pea,
                        resolve_flow_power_pricing_context,
                    )
                    provider = config_entry.options.get(
                        CONF_ELECTRICITY_PROVIDER,
                        config_entry.data.get(CONF_ELECTRICITY_PROVIDER, ""),
                    )
                    if provider == "flow_power":
                        pea_enabled = config_entry.options.get(CONF_PEA_ENABLED, True)
                        fp_base_rate = config_entry.options.get(
                            CONF_FLOW_POWER_BASE_RATE, FLOW_POWER_DEFAULT_BASE_RATE
                        )
                        fp_custom_pea = config_entry.options.get(CONF_PEA_CUSTOM_VALUE)
                        try:
                            fp_custom_pea_value = (
                                float(fp_custom_pea)
                                if fp_custom_pea not in (None, "")
                                else None
                            )
                        except (TypeError, ValueError):
                            fp_custom_pea_value = None
                        if fp_custom_pea_value is not None:
                            pea = fp_custom_pea_value
                        elif pea_enabled:
                            pricing = resolve_flow_power_pricing_context(
                                config_entry.options,
                                config_entry.data,
                                entry_data,
                            )
                            pea = calculate_flow_power_pea(
                                wholesale_cents,
                                pricing,
                                tariff_rate=entry_data.get("fp_tariff_rate"),
                                avg_daily_tariff=entry_data.get("fp_avg_daily_tariff"),
                            )
                        else:
                            pea = 0.0
                        buy_cents_fp = max(0.0, fp_base_rate + pea)
                        # Export: Flow Power pays a flat happy hour rate, not the AEMO spot price.
                        # The AEMO feedIn channel reflects the wholesale price, which is unrelated
                        # to the fixed 45c/kWh happy hour credit Flow Power actually pays.
                        fp_state = config_entry.options.get(
                            CONF_FLOW_POWER_STATE,
                            config_entry.data.get(CONF_FLOW_POWER_STATE, "QLD1"),
                        )
                        now_local = dt_util.now()
                        period_key = f"PERIOD_{now_local.hour:02d}_{(now_local.minute // 30) * 30:02d}"
                        sell_dollar_fp = (
                            _flow_power_export_rate_dollars(config_entry, fp_state)
                            if period_key in FLOW_POWER_HAPPY_HOUR_PERIODS
                            else 0.0
                        )
                        return (buy_cents_fp / 100.0, sell_dollar_fp)
                    else:
                        # Generic AEMO (non-Flow-Power): wholesale price is the retail price
                        buy_dollar = wholesale_cents / 100.0
                        sell_dollar = max(0.0, -(sell_cents_raw or 0)) / 100.0
                        return (buy_dollar, sell_dollar)

        # Fall back to tariff schedule (TOU rates).
        # Note: buy_prices/sell_prices in tariff_schedule are stored in $/kWh (Tesla
        # tariff format). get_current_price_from_tariff_schedule() multiplies by 100
        # internally for the PERIOD_HH_MM branch, so the return value is always c/kWh.
        tariff_schedule = entry_data.get("tariff_schedule")
        if tariff_schedule:
            from . import get_current_price_from_tariff_schedule
            buy_cents, sell_cents, _ = get_current_price_from_tariff_schedule(tariff_schedule)
            return (buy_cents / 100.0, sell_cents / 100.0)

    except Exception as exc:
        _LOGGER.debug("Failed to get current prices for cost tracking: %s", exc)

    return (None, None)


class SensitiveDataFilter(logging.Filter):
    """
    Logging filter that obfuscates sensitive data like API keys and tokens.
    Shows first 4 and last 4 characters with asterisks in between.
    """

    @staticmethod
    def obfuscate(value: str, show_chars: int = 4) -> str:
        """Obfuscate a string showing only first and last N characters."""
        if len(value) <= show_chars * 2:
            return '*' * len(value)
        return f"{value[:show_chars]}{'*' * (len(value) - show_chars * 2)}{value[-show_chars:]}"

    def _obfuscate_string(self, text: str) -> str:
        """Apply all obfuscation patterns to a string."""
        if not text:
            return text

        # Handle Bearer tokens
        text = re.sub(
            r'(Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle psk_ tokens (Amber API keys)
        text = re.sub(
            r'(psk_)([a-zA-Z0-9]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle authorization headers in websocket/API logs
        text = re.sub(
            r'(authorization:\s*Bearer\s+)([a-zA-Z0-9_-]{20,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle site IDs (alphanumeric, like Amber 01KAR0YMB7JQDVZ10SN1SGA0CV)
        text = re.sub(
            r'(site[_\s]?[iI][dD]["\']?[\s:=]+["\']?)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text
        )

        # Handle "for site {id}" pattern
        text = re.sub(
            r'(for site\s+)([a-zA-Z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle email addresses
        text = re.sub(
            r'([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})',
            lambda m: self.obfuscate(m.group(1)),
            text
        )

        # Handle Tesla energy site IDs (numeric, 13-20 digits) - in URLs and JSON
        text = re.sub(
            r'(energy_site[s]?[/\s:=]+["\']?)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle standalone long numeric IDs (Tesla energy site IDs in various contexts)
        text = re.sub(
            r'(\bsite\s+)(\d{13,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers in JSON format ('vin': 'XXX' or "vin": "XXX")
        text = re.sub(
            r'(["\']vin["\']:\s*["\'])([A-HJ-NPR-Z0-9]{17})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle VIN numbers plain format
        text = re.sub(
            r'(\bvin[\s:=]+)([A-HJ-NPR-Z0-9]{17})\b',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )
        text = obfuscate_vin_tokens(text, self.obfuscate)

        # Handle DIN numbers in JSON format
        text = re.sub(
            r'(["\']din["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle DIN numbers plain format
        text = re.sub(
            r'(\bdin[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers in JSON format
        text = re.sub(
            r'(["\']serial_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle serial numbers plain format
        text = re.sub(
            r'(serial[\s_]?(?:number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs in JSON format
        text = re.sub(
            r'(["\']gateway_id["\']:\s*["\'])([A-Za-z0-9-]{15,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle gateway IDs plain format
        text = re.sub(
            r'(gateway[\s_]?(?:id)?[\s:=]+["\']?)([A-Za-z0-9-]{15,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers in JSON format
        text = re.sub(
            r'(["\']warp_site_number["\']:\s*["\'])([A-Za-z0-9-]{8,})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle warp site numbers plain format
        text = re.sub(
            r'(warp[\s_]?(?:site)?(?:[\s_]?number)?[\s:=]+["\']?)([A-Za-z0-9-]{8,})',
            lambda m: m.group(1) + self.obfuscate(m.group(2)),
            text,
            flags=re.IGNORECASE
        )

        # Handle asset_site_id (UUIDs)
        text = re.sub(
            r'(["\']asset_site_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # Handle device_id (UUIDs)
        text = re.sub(
            r'(["\']device_id["\']:\s*["\'])([a-f0-9-]{36})(["\'])',
            lambda m: m.group(1) + self.obfuscate(m.group(2)) + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        return text

    def _obfuscate_arg(self, arg: Any) -> Any:
        """Obfuscate an argument only if it contains sensitive data, preserving type otherwise."""
        # Convert to string for pattern matching
        str_value = str(arg)
        obfuscated = self._obfuscate_string(str_value)

        # Only return string version if obfuscation actually changed something
        # This preserves numeric types for format specifiers like %d and %.3f
        if obfuscated != str_value:
            return obfuscated
        return arg

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter log record to obfuscate sensitive data."""
        # Handle the message
        if record.msg:
            record.msg = self._obfuscate_string(str(record.msg))

        # Handle args if present (for %-style formatting)
        # Only convert args to strings if obfuscation patterns match
        # This preserves numeric types for format specifiers like %d and %.3f
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._obfuscate_arg(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._obfuscate_arg(a) for a in record.args)

        return True


_LOGGER = logging.getLogger(__name__)
_LOGGER.addFilter(SensitiveDataFilter())


def _parse_retry_after(response: aiohttp.ClientResponse) -> float | None:
    """Parse Retry-After header from an HTTP response.

    Returns delay in seconds, or None if header is missing/invalid.
    Supports both delta-seconds and HTTP-date formats.
    """
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        # Try delta-seconds first (e.g. "30")
        return max(1.0, min(float(retry_after), 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        pass
    try:
        # Try HTTP-date format (e.g. "Tue, 11 Feb 2026 03:00:00 GMT")
        from email.utils import parsedate_to_datetime
        retry_date = parsedate_to_datetime(retry_after)
        from homeassistant.util import dt as dt_util
        delay = (retry_date - dt_util.utcnow()).total_seconds()
        return max(1.0, min(delay, 300.0))  # Clamp 1-300s
    except (ValueError, TypeError):
        return None


async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict,
    max_retries: int = 3,
    timeout_seconds: int = 60,
    raise_auth_failed: bool = True,
    **kwargs
) -> dict[str, Any]:
    """Fetch data with exponential backoff retry logic.

    Respects Retry-After headers from 429/503 responses. Retries on
    5xx server errors and 429 rate limits; fails immediately on other 4xx.

    Args:
        session: aiohttp client session
        url: URL to fetch
        headers: Request headers
        max_retries: Maximum number of retry attempts (default: 3)
        timeout_seconds: Request timeout in seconds (default: 60)
        raise_auth_failed: Whether 401 responses should raise
            ConfigEntryAuthFailed instead of UpdateFailed
        **kwargs: Additional arguments to pass to session.get()

    Returns:
        JSON response data

    Raises:
        UpdateFailed: If all retries fail
    """
    last_error = None
    retry_after_delay = None  # Set by Retry-After header

    for attempt in range(max_retries):
        try:
            if attempt > 0:
                # Use Retry-After delay if available, otherwise exponential backoff
                wait_time = retry_after_delay or (2 ** attempt)
                retry_after_delay = None  # Reset for next attempt
                _LOGGER.info(
                    "Retry attempt %d/%d after %.0fs delay",
                    attempt + 1, max_retries, wait_time,
                )
                await asyncio.sleep(wait_time)

            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                **kwargs
            ) as response:
                if response.status == 200:
                    return await response.json()

                error_text = await response.text()

                if response.status == 429:
                    # Rate limited — retry with Retry-After if provided
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Rate limited 429 (attempt %d/%d): %s (retry-after: %s)",
                        attempt + 1, max_retries, error_text[:200],
                        f"{retry_after_delay:.0f}s" if retry_after_delay else "not set",
                    )
                    last_error = UpdateFailed(f"Rate limited: 429")
                    continue

                if response.status >= 500:
                    # Server error — retry, respect Retry-After if present
                    retry_after_delay = _parse_retry_after(response)
                    _LOGGER.warning(
                        "Server error (attempt %d/%d): %s - %s",
                        attempt + 1, max_retries, response.status, error_text[:200],
                    )
                    last_error = UpdateFailed(f"Server error: {response.status}")
                    continue

                # 401 → token expired/revoked. Direct token providers should
                # trigger HA reauth. Fleet API tokens are owned/refreshed by
                # the separate tesla_fleet integration, so callers can treat
                # them as transient stale-token failures instead.
                if response.status == 401:
                    if raise_auth_failed:
                        _LOGGER.warning(
                            "Authentication failed (401) — triggering reauth: %s",
                            error_text[:200],
                        )
                        raise ConfigEntryAuthFailed(f"Token rejected by upstream: {error_text[:200]}")
                    _LOGGER.warning(
                        "Authentication failed (401) — token may be refreshing upstream: %s",
                        error_text[:200],
                    )
                    raise UpdateFailed(f"Authentication failed: 401 - {error_text[:200]}")

                # Other 4xx client errors — don't retry
                raise UpdateFailed(f"Client error {response.status}: {error_text}")

        except aiohttp.ClientError as err:
            _LOGGER.warning(
                "Network error (attempt %d/%d): %s",
                attempt + 1, max_retries, err,
            )
            last_error = UpdateFailed(f"Network error: {err}")
            continue

        except asyncio.TimeoutError:
            _LOGGER.warning(
                "Timeout error (attempt %d/%d): Request exceeded %ds",
                attempt + 1, max_retries, timeout_seconds,
            )
            last_error = UpdateFailed(f"Timeout after {timeout_seconds}s")
            continue

    # All retries failed
    raise last_error or UpdateFailed("All retry attempts failed")


def _merge_amber_forecasts(forecast_5min: list, forecast_30min: list) -> list:
    """Merge 5-min near-term with 30-min extended horizon, avoiding overlap.

    5-min data covers today at NEM dispatch resolution; 30-min extends ~40h.
    We keep all 5-min entries and only append 30-min entries that start at or
    after the latest 5-min interval end (nemTime).
    """
    if not forecast_5min:
        return forecast_30min or []
    if not forecast_30min:
        return forecast_5min or []

    # Find latest nemTime (interval END) in 5-min data
    latest_5min_end = max(
        (e.get("nemTime", "") for e in forecast_5min),
        default="",
    )
    if not latest_5min_end:
        return forecast_30min

    try:
        boundary = datetime.fromisoformat(latest_5min_end.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return forecast_30min

    # Keep only 30-min entries whose start is at or after the boundary
    filtered_30min = []
    for entry in forecast_30min:
        nem = entry.get("nemTime", "")
        dur = entry.get("duration", 30)
        if nem:
            try:
                end = datetime.fromisoformat(nem.replace("Z", "+00:00"))
                start = end - timedelta(minutes=dur)
                if start >= boundary:
                    filtered_30min.append(entry)
            except (ValueError, TypeError):
                filtered_30min.append(entry)  # keep if unparseable

    return list(forecast_5min) + filtered_30min


class AmberPriceCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Amber electricity price data."""

    _FORECAST_5MIN_TTL = timedelta(minutes=4, seconds=30)
    _FORECAST_30MIN_TTL = timedelta(minutes=30)

    def __init__(
        self,
        hass: HomeAssistant,
        api_token: str,
        site_id: str | None = None,
        ws_client=None,
    ) -> None:
        """Initialize the coordinator."""
        self.api_token = api_token
        self.site_id = site_id
        self.session = async_get_clientsession(hass)
        self.ws_client = ws_client  # WebSocket client for real-time prices
        self._forecast_5min_cache: list[dict[str, Any]] | None = None
        self._forecast_5min_fetched_at: datetime | None = None
        self._forecast_30min_cache: list[dict[str, Any]] | None = None
        self._forecast_30min_fetched_at: datetime | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_amber_prices",
            update_interval=UPDATE_INTERVAL_PRICES,
        )

    async def _fetch_forecast_with_cache(
        self,
        *,
        url: str,
        headers: dict[str, str],
        params: dict[str, Any],
        label: str,
        ttl: timedelta,
        cache_attr: str,
        fetched_at_attr: str,
    ) -> list[dict[str, Any]]:
        """Fetch Amber forecast data, reusing cached data within the TTL."""
        cached = getattr(self, cache_attr)
        fetched_at = getattr(self, fetched_at_attr)
        now = dt_util.utcnow()

        if cached is not None and fetched_at is not None and now - fetched_at < ttl:
            age_seconds = (now - fetched_at).total_seconds()
            _LOGGER.debug(
                "Using cached Amber %s forecast (age %.0fs, ttl %.0fs)",
                label,
                age_seconds,
                ttl.total_seconds(),
            )
            return cached

        try:
            forecast = await _fetch_with_retry(
                self.session,
                url,
                headers,
                params=params,
                max_retries=2,
                timeout_seconds=30,
            )
        except UpdateFailed:
            if cached is not None:
                age_minutes = (
                    (now - fetched_at).total_seconds() / 60
                    if fetched_at is not None
                    else -1
                )
                _LOGGER.warning(
                    "Amber %s forecast refresh failed; using cached data (age %.1fm)",
                    label,
                    age_minutes,
                )
                return cached
            raise

        setattr(self, cache_attr, forecast or [])
        setattr(self, fetched_at_attr, now)
        return forecast or []

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Amber API with WebSocket-first approach."""
        headers = {"Authorization": f"Bearer {self.api_token}"}

        try:
            # Try WebSocket first for current prices (real-time, low latency)
            current_prices = None
            if self.ws_client:
                # Retry logic: Try for 10 seconds with 2-second intervals (5 attempts)
                max_age_seconds = 60  # Reduced from 360s to 60s for fresher data
                retry_attempts = 5
                retry_interval = 2  # seconds

                for attempt in range(retry_attempts):
                    current_prices = self.ws_client.get_latest_prices(max_age_seconds=max_age_seconds)

                    if current_prices:
                        # Get health status to log data age
                        health = self.ws_client.get_health_status()
                        age = health.get('age_seconds', 'unknown')
                        _LOGGER.info(f"✓ Using WebSocket prices (age: {age}s, attempt: {attempt + 1}/{retry_attempts})")
                        break

                    # If not last attempt, wait before retry
                    if attempt < retry_attempts - 1:
                        _LOGGER.debug(f"WebSocket data unavailable/stale, retrying in {retry_interval}s (attempt {attempt + 1}/{retry_attempts})")
                        await asyncio.sleep(retry_interval)

                # All retries exhausted
                if not current_prices:
                    _LOGGER.info(f"WebSocket prices unavailable after {retry_attempts} attempts ({max_age_seconds}s staleness threshold), falling back to REST API")

            # Fall back to REST API if WebSocket unavailable
            if not current_prices:
                _LOGGER.info("⚠ Using REST API for current prices (WebSocket unavailable)")
                current_prices = await _fetch_with_retry(
                    self.session,
                    f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices/current",
                    headers,
                    max_retries=2,  # Less retries for Amber (usually more reliable)
                    timeout_seconds=30,
                )

            # Dual-resolution forecast approach to ensure complete data coverage:
            # 1. Fetch today's 5-min data for CurrentInterval spike detection
            # 2. Fetch forecast at 30-min resolution via /prices/current for full
            #    AEMO horizon (~40h). The `next` param only works on /prices/current,
            #    not /prices (which is date-range based and ignores `next`).

            # Step 1: Get 5-min resolution data for current period spike detection
            forecast_5min = await self._fetch_forecast_with_cache(
                url=f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices",
                headers=headers,
                params={"resolution": 5},
                label="5-minute",
                ttl=self._FORECAST_5MIN_TTL,
                cache_attr="_forecast_5min_cache",
                fetched_at_attr="_forecast_5min_fetched_at",
            )

            # Step 2: Get 30-min forecast via /prices/current (supports `next`)
            # Request 288 intervals (144h) — API returns whatever AEMO has (~40h)
            forecast_30min = await self._fetch_forecast_with_cache(
                url=f"{AMBER_API_BASE_URL}/sites/{self.site_id}/prices/current",
                headers=headers,
                params={"next": 288, "resolution": 30},
                label="30-minute",
                ttl=self._FORECAST_30MIN_TTL,
                cache_attr="_forecast_30min_cache",
                fetched_at_attr="_forecast_30min_fetched_at",
            )

            return {
                "current": current_prices,
                "forecast": _merge_amber_forecasts(forecast_5min, forecast_30min),
                "forecast_5min": forecast_5min,  # Keep for TOU sync spike detection
                "last_update": dt_util.utcnow(),
            }

        except UpdateFailed:
            raise  # Re-raise UpdateFailed exceptions
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching Amber data: {err}") from err


# ============================================================
# Localvolts Price Coordinator
# ============================================================

def _parse_localvolts_price(value) -> float:
    """Parse a Localvolts price value, handling 'N/A' and non-numeric values.

    Returns price in c/kWh (same unit as Amber perKwh).
    """
    if value is None or value == "N/A" or value == "n/a":
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def _localvolts_interval_start(interval_end: str, duration_minutes: int = 5) -> str:
    """Calculate interval start time from interval end time.

    Args:
        interval_end: ISO 8601 datetime string for interval end
        duration_minutes: Duration of interval in minutes (default 5)

    Returns:
        ISO 8601 datetime string for interval start
    """
    try:
        end_dt = datetime.fromisoformat(interval_end.replace("Z", "+00:00"))
        start_dt = end_dt - timedelta(minutes=duration_minutes)
        return start_dt.isoformat()
    except (ValueError, TypeError):
        return interval_end


class LocalvoltsPriceCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Localvolts electricity price data.

    Converts Localvolts API data to Amber-compatible format so all downstream
    code (LP optimizer, sensors, TOU sync, curtailment) works unchanged.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        partner_id: str,
        nmi: str,
    ) -> None:
        """Initialize the coordinator."""
        from .localvolts_api import LocalvoltsClient

        self.client = LocalvoltsClient(
            async_get_clientsession(hass), api_key, partner_id
        )
        self.nmi = nmi

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_localvolts_prices",
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Localvolts API and convert to Amber-compatible format."""
        try:
            intervals = await self.client.get_intervals(self.nmi)

            if not intervals:
                raise UpdateFailed("No interval data returned from Localvolts API")

            current_prices = []
            forecast_prices = []

            for interval in intervals:
                nem_time = interval.get("intervalEnd", "")
                quality = interval.get("quality", "Fcst")

                # Import price: costsFlexUp (c/kWh)
                import_ckwh = _parse_localvolts_price(interval.get("costsFlexUp"))
                # Export price: earningsFlexUp (c/kWh)
                # Negate to match Amber convention: Amber feedIn.perKwh is negative
                # when earning; Localvolts earningsFlexUp is positive when earning
                export_ckwh = -_parse_localvolts_price(interval.get("earningsFlexUp"))

                start_time = _localvolts_interval_start(nem_time, 5)

                general_entry = {
                    "nemTime": nem_time,
                    "perKwh": import_ckwh,
                    "channelType": "general",
                    "type": "CurrentInterval" if quality in ("Act", "Exp") else "ForecastInterval",
                    "duration": 5,
                    "startTime": start_time,
                }
                feedin_entry = {
                    "nemTime": nem_time,
                    "perKwh": export_ckwh,
                    "channelType": "feedIn",
                    "type": general_entry["type"],
                    "duration": 5,
                    "startTime": start_time,
                }

                if quality in ("Act", "Exp"):
                    current_prices.extend([general_entry, feedin_entry])
                else:
                    forecast_prices.extend([general_entry, feedin_entry])

            _LOGGER.debug(
                "Localvolts data: %d current entries, %d forecast entries",
                len(current_prices),
                len(forecast_prices),
            )

            return {
                "current": current_prices,
                "forecast": forecast_prices,
                "last_update": dt_util.utcnow(),
            }

        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching Localvolts data: {err}") from err


# ============================================================
# Amber Usage API — actual metered cost data from NEM
# ============================================================

USAGE_FETCH_INTERVAL = timedelta(hours=4)
USAGE_STORAGE_VERSION = 2  # v2: costs in dollars (v1 had cents-as-dollars bug)
USAGE_STORAGE_KEY = "power_sync.amber_usage"
USAGE_MAX_DAYS = 365
AMBER_DEFAULT_MONTHLY_SUPPLY_FEE = 25.0  # Amber's standard $25/month supply charge

# Quality ranking for deciding whether to overwrite existing data
_QUALITY_RANK = {"estimated": 0, "mixed": 1, "billable": 2}


@dataclass
class DayUsage:
    """Actual metered usage and cost for a single day from Amber."""

    date: str                   # "YYYY-MM-DD"
    import_kwh: float           # general channel total
    export_kwh: float           # feedIn channel (absolute)
    controlled_load_kwh: float
    import_cost: float          # $ gross import
    export_earnings: float      # $ gross export earnings
    net_cost: float             # import_cost - export_earnings
    quality: str                # "estimated", "billable", or "mixed"


class AmberUsageCoordinator:
    """Fetches actual metered usage/cost from the Amber Usage API.

    Not a DataUpdateCoordinator — usage data updates infrequently (every 4h).
    Uses HA Store for persistence across restarts.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api_token: str,
        site_id: str,
        entry_id: str,
        monthly_supply_fee: float = AMBER_DEFAULT_MONTHLY_SUPPLY_FEE,
    ) -> None:
        """Initialize the Amber usage coordinator."""
        self.hass = hass
        self._api_token = api_token
        self._site_id = site_id
        self._entry_id = entry_id
        self._monthly_supply_fee = monthly_supply_fee
        self._session = async_get_clientsession(hass)
        self._store = Store(hass, USAGE_STORAGE_VERSION, f"{USAGE_STORAGE_KEY}.{entry_id}")

        # In-memory state
        self._days: dict[str, DayUsage] = {}
        self._baselines: dict[str, float] = {}  # date → baseline_cost from optimizer
        self._last_fetch: datetime | None = None
        self._cancel_timer: Any = None
        self._cancel_initial: Any = None

    @property
    def last_fetch_iso(self) -> str | None:
        """Return the last fetch time as ISO string."""
        return self._last_fetch.isoformat() if self._last_fetch else None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Load stored data and schedule periodic fetches."""
        await self._load_store()
        # Delay initial fetch 30-90s to avoid competing with price coordinator
        # at startup for Amber API rate limit budget
        import random
        delay = 30 + random.randint(0, 60)
        _LOGGER.info("Amber usage: first fetch in %ds (avoiding startup rate limit contention)", delay)
        self._cancel_initial = self.hass.loop.call_later(
            delay, lambda: self.hass.async_create_task(self._fetch_usage())
        )
        from homeassistant.helpers.event import async_track_time_interval
        self._cancel_timer = async_track_time_interval(
            self.hass, self._scheduled_fetch, USAGE_FETCH_INTERVAL
        )

    async def async_stop(self) -> None:
        """Cancel the periodic timer and any pending initial fetch."""
        if self._cancel_initial:
            self._cancel_initial.cancel()
            self._cancel_initial = None
        if self._cancel_timer:
            self._cancel_timer()
            self._cancel_timer = None

    async def _scheduled_fetch(self, _now=None) -> None:
        """Timer callback for periodic fetch."""
        await self._fetch_usage()

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    async def _load_store(self) -> None:
        """Load persisted usage data from HA Store."""
        try:
            stored = await self._store.async_load()
        except Exception as e:
            _LOGGER.warning("Amber usage: store load failed (will re-fetch): %s", e)
            stored = None
        if not stored:
            _LOGGER.info("Amber usage: no stored data (fresh start or version upgrade)")
            return
        for day_dict in stored.get("days", []):
            try:
                du = DayUsage(**day_dict)
                self._days[du.date] = du
            except (TypeError, KeyError):
                continue
        self._baselines = stored.get("baselines", {})
        last_ts = stored.get("last_fetch")
        if last_ts:
            try:
                self._last_fetch = datetime.fromisoformat(last_ts)
            except (ValueError, TypeError):
                pass
        _LOGGER.info("Amber usage: restored %d days from store", len(self._days))

    def _save_store(self) -> None:
        """Persist current data to HA Store (delayed write)."""
        data = {
            "days": [asdict(du) for du in self._days.values()],
            "baselines": self._baselines,
            "last_fetch": self._last_fetch.isoformat() if self._last_fetch else None,
        }
        self._store.async_delay_save(lambda: data, 60)

    # ------------------------------------------------------------------
    # API fetch
    # ------------------------------------------------------------------

    async def _fetch_usage(self) -> None:
        """Fetch usage data from Amber API.

        Uses _fetch_with_retry for consistent 429/retry handling with the
        price coordinator. Checks RateLimit-Remaining header proactively
        and skips the fetch if the budget is low, to avoid starving the
        more important real-time price fetches.

        Amber Usage API has a 7-day max range per request, so large
        back-fills are batched into 7-day chunks.
        """
        now = dt_util.now()
        today = now.date()

        # Determine date range
        if not self._days:
            # First run — fetch 90 days of history
            start_date = today - timedelta(days=90)
        else:
            # Subsequent runs — re-fetch last 3 days for quality upgrades
            start_date = today - timedelta(days=3)

        end_date = today

        headers = {"Authorization": f"Bearer {self._api_token}"}

        # Pre-flight: probe rate limit budget with a lightweight check.
        # If RateLimit-Remaining is low, skip this non-critical fetch
        # to preserve budget for the real-time price coordinator.
        try:
            async with self._session.get(
                f"{AMBER_API_BASE_URL}/sites/{self._site_id}/prices/current",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as probe_resp:
                remaining = probe_resp.headers.get("RateLimit-Remaining")
                if remaining is not None:
                    try:
                        remaining_int = int(remaining)
                        if remaining_int < 10:
                            _LOGGER.info(
                                "Amber usage: skipping fetch — only %d API calls remaining "
                                "(preserving budget for price updates)",
                                remaining_int,
                            )
                            return
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass  # Probe failed — proceed with fetch anyway

        # Amber Usage API allows max 7-day range per request — batch accordingly
        total_updated = 0
        chunk_start = start_date
        url = f"{AMBER_API_BASE_URL}/sites/{self._site_id}/usage"

        while chunk_start <= end_date:
            chunk_end = min(chunk_start + timedelta(days=6), end_date)
            params = {
                "startDate": chunk_start.isoformat(),
                "endDate": chunk_end.isoformat(),
                "resolution": "30",
            }

            try:
                intervals = await _fetch_with_retry(
                    self._session,
                    url,
                    headers,
                    max_retries=2,
                    timeout_seconds=30,
                    params=params,
                )
                updated = self._process_intervals(intervals)
                total_updated += updated
                _LOGGER.debug(
                    "Amber usage chunk %s to %s: %d days updated",
                    chunk_start, chunk_end, updated,
                )
            except UpdateFailed as err:
                _LOGGER.warning("Amber usage fetch failed for %s to %s: %s", chunk_start, chunk_end, err)
            except Exception as err:
                _LOGGER.warning("Amber usage fetch failed unexpectedly for %s to %s: %s", chunk_start, chunk_end, err)

            chunk_start = chunk_end + timedelta(days=1)

        self._last_fetch = now
        self._prune_old_days()
        self._save_store()
        _LOGGER.info("Amber usage fetched: %d days updated (range %s to %s)", total_updated, start_date, end_date)

    def _process_intervals(self, intervals: list[dict]) -> int:
        """Aggregate 30-min intervals into daily DayUsage records.

        Returns count of days updated.
        """
        # Group by date and channel
        day_buckets: dict[str, dict[str, list[dict]]] = {}
        for iv in intervals:
            dt_str = iv.get("nemTime") or iv.get("startTime") or ""
            try:
                day_key = dt_str[:10]  # "YYYY-MM-DD"
                # Validate it's a real date
                date.fromisoformat(day_key)
            except (ValueError, IndexError):
                continue
            channel = iv.get("channelType", "general")
            day_buckets.setdefault(day_key, {}).setdefault(channel, []).append(iv)

        updated = 0
        for day_key, channels in day_buckets.items():
            import_kwh = 0.0
            export_kwh = 0.0
            controlled_kwh = 0.0
            import_cost = 0.0
            export_earnings = 0.0
            qualities: set[str] = set()

            for iv in channels.get("general", []):
                kwh = abs(iv.get("kwh", 0))
                import_kwh += kwh
                # Amber API returns cost in cents — convert to dollars
                import_cost += iv.get("cost", 0) / 100
                qualities.add(iv.get("quality", "estimated"))

            for iv in channels.get("feedIn", []):
                kwh = abs(iv.get("kwh", 0))
                export_kwh += kwh
                # Amber feedIn cost: negative = you earned, positive = you paid to export
                # Negate so earnings are positive when earning, negative when paying
                export_earnings += -iv.get("cost", 0) / 100
                qualities.add(iv.get("quality", "estimated"))

            for iv in channels.get("controlledLoad", []):
                kwh = abs(iv.get("kwh", 0))
                controlled_kwh += kwh
                import_cost += iv.get("cost", 0) / 100
                qualities.add(iv.get("quality", "estimated"))

            if "billable" in qualities and "estimated" in qualities:
                quality = "mixed"
            elif "billable" in qualities:
                quality = "billable"
            else:
                quality = "estimated"

            new_du = DayUsage(
                date=day_key,
                import_kwh=round(import_kwh, 3),
                export_kwh=round(export_kwh, 3),
                controlled_load_kwh=round(controlled_kwh, 3),
                import_cost=round(import_cost, 4),
                export_earnings=round(export_earnings, 4),
                net_cost=round(import_cost - export_earnings, 4),
                quality=quality,
            )

            # Only overwrite if new data is same or better quality
            existing = self._days.get(day_key)
            if existing:
                existing_rank = _QUALITY_RANK.get(existing.quality, 0)
                new_rank = _QUALITY_RANK.get(quality, 0)
                if new_rank < existing_rank:
                    continue  # Don't downgrade quality

            self._days[day_key] = new_du
            updated += 1

        return updated

    def _prune_old_days(self) -> None:
        """Remove days older than USAGE_MAX_DAYS to limit storage."""
        cutoff = (dt_util.now().date() - timedelta(days=USAGE_MAX_DAYS)).isoformat()
        old_keys = [k for k in self._days if k < cutoff]
        for k in old_keys:
            del self._days[k]
        # Also prune baselines
        old_baselines = [k for k in self._baselines if k < cutoff]
        for k in old_baselines:
            del self._baselines[k]

    # ------------------------------------------------------------------
    # Baseline recording (called from optimization coordinator at midnight)
    # ------------------------------------------------------------------

    def record_baseline(self, date_str: str, baseline_cost: float) -> None:
        """Record the optimizer's baseline cost for a completed day."""
        self._baselines[date_str] = round(baseline_cost, 4)
        self._save_store()
        _LOGGER.info("Amber usage: recorded baseline $%.2f for %s", baseline_cost, date_str)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def get_summary(self, period: str) -> dict[str, Any]:
        """Get aggregated usage for a period.

        period: 'yesterday', 'week' (last 7 complete days), 'month' (calendar month to yesterday), 'last_month'
        """
        days = self._get_days_for_period(period)
        return self._aggregate(days)

    def get_savings_summary(self, period: str) -> dict[str, Any]:
        """Get aggregated usage with baseline and savings for a period."""
        days = self._get_days_for_period(period)
        result = self._aggregate(days)

        # Add baseline and savings.
        # Savings = baseline_energy - actual_energy (supply charge excluded
        # from savings calc since it's a fixed cost with or without battery).
        # Baseline includes supply charge so it reflects true "no battery" cost.
        baseline_total = 0.0
        baseline_days = 0
        supply_total = sum(self._daily_supply_fee(du.date) for du in days)
        for du in days:
            bl = self._baselines.get(du.date)
            if bl is not None:
                baseline_total += bl
                baseline_days += 1

        result["baseline_cost"] = round(baseline_total + supply_total, 2) if baseline_days > 0 else None
        result["savings"] = round(baseline_total - (result["net_cost"] - result["supply_charge"]), 2) if baseline_days > 0 else None
        result["baseline_days"] = baseline_days
        return result

    def get_range(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Get day-by-day data for a custom date range."""
        result = []
        for day_key in sorted(self._days.keys()):
            if start_date <= day_key <= end_date:
                du = self._days[day_key]
                d = asdict(du)
                daily_fee = self._daily_supply_fee(day_key)
                d["supply_charge"] = round(daily_fee, 2)
                d["net_cost"] = round(du.net_cost + daily_fee, 2)
                bl = self._baselines.get(day_key)
                d["baseline_cost"] = bl
                d["savings"] = round(bl - d["net_cost"], 2) if bl is not None else None
                result.append(d)
        return result

    def _get_days_for_period(self, period: str) -> list[DayUsage]:
        """Return list of DayUsage records for the given period."""
        today = dt_util.now().date()
        yesterday = today - timedelta(days=1)

        if period == "yesterday":
            key = yesterday.isoformat()
            du = self._days.get(key)
            return [du] if du else []
        elif period == "week":
            start = (today - timedelta(days=7)).isoformat()
            end = yesterday.isoformat()
        elif period == "month":
            start = today.replace(day=1).isoformat()
            end = yesterday.isoformat()
        elif period == "last_month":
            first_this_month = today.replace(day=1)
            last_day_prev = first_this_month - timedelta(days=1)
            start = last_day_prev.replace(day=1).isoformat()
            end = last_day_prev.isoformat()
        else:
            return []

        return [
            self._days[k] for k in sorted(self._days.keys())
            if start <= k <= end
        ]

    def _daily_supply_fee(self, date_str: str) -> float:
        """Calculate the daily supply fee for a given date.

        Pro-rates the monthly fee by the actual number of days in that month
        so monthly totals always sum to exactly the monthly fee.
        """
        if self._monthly_supply_fee <= 0:
            return 0.0
        import calendar
        try:
            d = date.fromisoformat(date_str)
            days_in_month = calendar.monthrange(d.year, d.month)[1]
            return self._monthly_supply_fee / days_in_month
        except (ValueError, TypeError):
            return self._monthly_supply_fee / 30.0

    def _aggregate(self, days: list[DayUsage]) -> dict[str, Any]:
        """Aggregate a list of DayUsage into a summary dict.

        Includes the daily supply fee (pro-rated from monthly) in the totals.
        """
        if not days:
            return {
                "import_kwh": 0,
                "export_kwh": 0,
                "controlled_load_kwh": 0,
                "import_cost": 0,
                "export_earnings": 0,
                "supply_charge": 0,
                "net_cost": 0,
                "quality": "no_data",
                "days_count": 0,
            }
        qualities = set(du.quality for du in days)
        if len(qualities) == 1:
            quality = qualities.pop()
        elif "billable" in qualities and "estimated" in qualities:
            quality = "mixed"
        else:
            quality = "mixed"

        energy_cost = sum(du.net_cost for du in days)
        supply_charge = sum(self._daily_supply_fee(du.date) for du in days)

        return {
            "import_kwh": round(sum(du.import_kwh for du in days), 2),
            "export_kwh": round(sum(du.export_kwh for du in days), 2),
            "controlled_load_kwh": round(sum(du.controlled_load_kwh for du in days), 2),
            "import_cost": round(sum(du.import_cost for du in days), 2),
            "export_earnings": round(sum(du.export_earnings for du in days), 2),
            "supply_charge": round(supply_charge, 2),
            "net_cost": round(energy_cost + supply_charge, 2),
            "quality": quality,
            "days_count": len(days),
        }


class TeslaEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Tesla energy data from Tesla API (Teslemetry or Fleet API)."""

    def __init__(
        self,
        hass: HomeAssistant,
        site_id: str,
        api_token: str,
        api_provider: str = TESLA_PROVIDER_TESLEMETRY,
        token_getter: callable = None,
        entry_id: str = "",
        fleet_base_url: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            site_id: Tesla energy site ID
            api_token: Initial API token (used if token_getter not provided)
            api_provider: API provider (teslemetry or fleet_api)
            token_getter: Optional callable that returns (token, provider) tuple.
                          If provided, this is called before each request to get fresh token.
            entry_id: Config entry ID for price lookups
            fleet_base_url: Regional Fleet API base URL override (EU/AP users).
                            Stored in entry.data[CONF_FLEET_API_BASE_URL].
        """
        self.site_id = site_id
        self._api_token = api_token  # Fallback token
        self._token_getter = token_getter  # Callable to get fresh token
        self.api_provider = api_provider
        self._entry_id = entry_id
        self._fleet_base_url = fleet_base_url  # Per-entry regional URL override
        self.session = async_get_clientsession(hass)
        self._site_info_cache = None  # Cache site_info (normally refreshed every 6 hours)
        self._site_info_last_fetch: float = 0  # Timestamp of last successful fetch
        self._site_info_fetch_failed = False  # Negative cache to avoid retrying on every sync cycle
        self._energy_acc = EnergyAccumulator(hass, "tesla")
        self._firmware = None  # Extracted from site_info gateways
        self._last_valid_battery_level_pct: float | None = None

        # Tesla Energy Site capability detection (populated by probe on first site_info fetch).
        # Keys: storm_mode, off_grid_vehicle_charging_reserve, vpp_programs.
        # Value True means the feature is supported by this site; False means unsupported
        # (either Tesla returned 4xx on probe, or the feature is not available in this country).
        self.tesla_capabilities: dict[str, bool] = {}
        self._capabilities_probed = False
        self._site_country: str | None = None  # From site_info (used to gate region-locked features)

        # Cached current-state values for new energy-site controls (populated opportunistically)
        self._storm_mode_enabled: bool | None = None
        self._off_grid_reserve_percent: int | None = None
        self._vpp_programs_cache: list[dict] | None = None

        # Grid status tracking (off-grid / islanding detection)
        self._last_grid_status: str = "Active"  # "Active" or "Islanded"

        # Tesla server outage tracking
        self._consecutive_failures: int = 0
        self._failure_streak_start: float = 0  # monotonic timestamp
        self._outage_notified: bool = False
        self._outage_start: float = 0  # monotonic timestamp
        self._last_outage_notification: float = 0  # monotonic timestamp (cooldown)

        # Lifetime energy totals (refreshed hourly from calendar_history period=lifetime)
        self._lifetime_totals: dict[str, float] | None = None
        self._lifetime_last_fetch: float = 0
        self._lifetime_fetch_failed: bool = False
        self._lifetime_totals_restored: bool = False
        self._lifetime_totals_store = Store(
            hass,
            LIFETIME_TOTALS_STORE_VERSION,
            f"power_sync.lifetime_totals.{entry_id or site_id}",
        )

        # Determine API base URL based on provider
        if api_provider == TESLA_PROVIDER_POWERSYNC:
            self.api_base_url = POWERSYNC_API_BASE_URL
            _LOGGER.info(f"TeslaEnergyCoordinator initialized with PowerSync.cc proxy for site {site_id}")
        elif api_provider == TESLA_PROVIDER_FLEET_API:
            self.api_base_url = fleet_base_url or FLEET_API_BASE_URL
            _LOGGER.info(f"TeslaEnergyCoordinator initialized with Fleet API for site {site_id} (base: {self.api_base_url})")
        else:
            self.api_base_url = TESLEMETRY_API_BASE_URL
            _LOGGER.info(f"TeslaEnergyCoordinator initialized with Teslemetry for site {site_id}")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_tesla_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    def _resolve_battery_level_pct(self, live_status: dict[str, Any]) -> float | None:
        """Return Tesla SOC, preserving the last valid value when omitted."""
        raw_soc = live_status.get("percentage_charged")
        if raw_soc is not None:
            try:
                soc = float(raw_soc)
            except (TypeError, ValueError):
                soc = None
            if soc is not None and 0 <= soc <= 100:
                self._last_valid_battery_level_pct = soc
                return soc

        if self._last_valid_battery_level_pct is not None:
            _LOGGER.debug(
                "Tesla live_status omitted percentage_charged; keeping last valid SOC %.1f%%",
                self._last_valid_battery_level_pct,
            )
            return self._last_valid_battery_level_pct

        _LOGGER.debug("Tesla live_status omitted percentage_charged and no cached SOC is available")
        return None

    def _record_tesla_update_failure(self, now: float) -> tuple[bool, float]:
        """Record a Tesla update failure and return whether to send outage notice."""
        self._consecutive_failures += 1
        if self._consecutive_failures == 1 or not self._failure_streak_start:
            self._failure_streak_start = now
        failure_duration = now - self._failure_streak_start
        should_notify = (
            self._consecutive_failures >= TESLA_OUTAGE_NOTIFY_FAILURES
            and failure_duration >= TESLA_OUTAGE_NOTIFY_MIN_SECONDS
            and not self._outage_notified
        )
        return should_notify, failure_duration

    def _get_current_token(self) -> str | None:
        """Get the current API token, fetching fresh if token_getter is available.

        Returns None if token_getter is set but returned no token — callers must
        treat this as a transient failure and raise UpdateFailed rather than
        falling back to the potentially stale startup token.
        """
        if self._token_getter:
            try:
                token, provider = self._token_getter()
                if token:
                    # Update provider and base URL if it changed
                    if provider != self.api_provider:
                        self.api_provider = provider
                        if provider == TESLA_PROVIDER_POWERSYNC:
                            self.api_base_url = POWERSYNC_API_BASE_URL
                        elif provider == TESLA_PROVIDER_FLEET_API:
                            self.api_base_url = self._fleet_base_url or FLEET_API_BASE_URL
                        else:
                            self.api_base_url = TESLEMETRY_API_BASE_URL
                        _LOGGER.debug("Token provider changed to %s", provider)
                    return token
                # token_getter returned None — fleet integration may be mid-refresh
                _LOGGER.warning("Token getter returned no token (fleet integration may be refreshing) — skipping poll")
                return None
            except Exception as e:
                _LOGGER.warning("Token getter failed — skipping poll: %s", e)
                return None
        return self._api_token

    def _coerce_lifetime_totals(self, data: Any) -> dict[str, float]:
        """Extract persisted lifetime totals as floats."""
        if not isinstance(data, dict):
            return {}
        totals: dict[str, float] = {}
        for key in LIFETIME_TOTAL_KEYS:
            value = data.get(key)
            if value is None:
                continue
            try:
                totals[key] = float(value)
            except (TypeError, ValueError):
                continue
        return totals

    def _clamp_lifetime_totals(self, totals: dict[str, float]) -> dict[str, float]:
        """Keep lifetime counters monotonic for total_increasing sensors."""
        previous = self._lifetime_totals or {}
        if not previous:
            return totals

        clamped = dict(totals)
        for key, value in totals.items():
            previous_value = previous.get(key)
            if previous_value is None or value >= previous_value:
                continue
            clamped[key] = previous_value
            _LOGGER.debug(
                "Keeping %s monotonic: Tesla reported %.3f kWh after %.3f kWh",
                key,
                value,
                previous_value,
            )
        return clamped

    async def async_restore_lifetime_totals(self) -> None:
        """Restore persisted lifetime totals before the first coordinator state."""
        if self._lifetime_totals_restored:
            return
        self._lifetime_totals_restored = True

        if not hasattr(self._lifetime_totals_store, "async_load"):
            return
        try:
            data = await self._lifetime_totals_store.async_load()
        except Exception as err:
            _LOGGER.warning("Failed to load persisted lifetime totals: %s", err)
            return

        totals = self._coerce_lifetime_totals(data)
        if not totals:
            return

        self._lifetime_totals = totals
        _LOGGER.info("Restored Tesla lifetime totals from storage")

    async def async_flush_lifetime_totals(self) -> None:
        """Persist lifetime totals so recorder-safe maxima survive restarts."""
        if not self._lifetime_totals or not hasattr(self._lifetime_totals_store, "async_save"):
            return
        await self._lifetime_totals_store.async_save(
            {key: round(value, 3) for key, value in self._lifetime_totals.items()}
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Tesla API (Teslemetry or Fleet API)."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        if not self._lifetime_totals_restored:
            await self.async_restore_lifetime_totals()

        current_token = self._get_current_token()
        if not current_token:
            raise UpdateFailed("Tesla token temporarily unavailable — will retry next poll")
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            # Get live status from Tesla API with retry logic
            # Note: Both Teslemetry and Fleet API can be slow, so we use retries
            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/live_status",
                headers,
                max_retries=3,  # More retries for reliability
                timeout_seconds=60,  # Longer timeout
                raise_auth_failed=self.api_provider != TESLA_PROVIDER_FLEET_API,
            )

            # Tesla returns {"response": null} occasionally during transient failures
            # or right after a token mint when the account state is still propagating.
            # Treat null/missing response as a temporary outage to avoid crashing.
            live_status = data.get("response") or {}
            _LOGGER.debug("Tesla API live_status response: %s", live_status)
            if not live_status:
                raise UpdateFailed("Tesla returned empty live_status response")

            # Extract EV charging power from Tesla Wall Connectors
            ev_power_kw = 0.0
            wall_connectors_raw = live_status.get("wall_connectors")
            if wall_connectors_raw:
                try:
                    # wall_connectors can be a JSON string or a list
                    if isinstance(wall_connectors_raw, str):
                        import ast
                        wall_connectors = ast.literal_eval(wall_connectors_raw)
                    else:
                        wall_connectors = wall_connectors_raw
                    for wc in wall_connectors:
                        wc_power = wc.get("wall_connector_power", 0) or 0
                        if wc_power > 0:
                            ev_power_kw += wc_power / 1000
                except Exception:
                    pass

            # Fallback: get EV power from BLE/Fleet vehicle sensors when
            # Wall Connector isn't reporting through Powerwall gateway.
            # Without this, EV charging power is counted as home load.
            if ev_power_kw == 0:
                try:
                    entry = self.hass.config_entries.async_get_entry(self._entry_id)
                    if entry:
                        from . import _get_ev_vehicle_status
                        ev_status = _get_ev_vehicle_status(self.hass, entry)
                        ev_power_kw = ev_status.get("ev_power_kw", 0) or 0
                except Exception:
                    pass

            # Map Teslemetry API response to our data structure
            solar_kw = live_status.get("solar_power", 0) / 1000
            grid_kw = live_status.get("grid_power", 0) / 1000
            battery_kw = live_status.get("battery_power", 0) / 1000
            load_kw = (live_status.get("load_power", 0) / 1000) - ev_power_kw

            # Accumulate daily energy from power readings (with cost tracking)
            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

            # Fetch site_info periodically to detect firmware updates (every 6 hours)
            _site_info_stale = (
                time.monotonic() - self._site_info_last_fetch
            ) > TESLA_SITE_INFO_CACHE_TTL_SECONDS
            if _site_info_stale and not self._site_info_fetch_failed:
                try:
                    await self.async_get_site_info()
                except Exception:
                    pass  # Non-critical, don't fail the update

            # Grid status: "Active" (on-grid) or "Islanded" (off-grid/blackout)
            grid_status = live_status.get("grid_status", "Active")

            # Detect grid status transitions and send push notifications.
            # Tesla API returns grid_status "Active" (on-grid) or "Inactive"
            # (off-grid). Only notify on real transitions, not initial load.
            is_on_grid = grid_status == "Active"
            prev_status = self._last_grid_status
            self._last_grid_status = grid_status
            if prev_status is not None and grid_status != prev_status:
                try:
                    from .automations.actions import _send_expo_push
                    if not is_on_grid:
                        _LOGGER.warning(
                            "Grid outage detected — Powerwall off-grid (site %s)",
                            self.site_id,
                        )
                        await _send_expo_push(
                            self.hass,
                            "Grid Outage Detected",
                            "Your Powerwall is running off-grid. Grid power is unavailable.",
                        )
                    else:
                        _LOGGER.info(
                            "Grid restored — Powerwall back on-grid (site %s)",
                            self.site_id,
                        )
                        await _send_expo_push(
                            self.hass,
                            "Grid Power Restored",
                            "Grid power has been restored. Your Powerwall is back on-grid.",
                        )
                except Exception:
                    pass

            # Derive the per-site nameplate power from cached site_info
            # (refreshed every 6 hours). Powerwall 2 is 5 kW continuous and
            # Powerwall 3 is 11.5 kW continuous; nameplate_power on Tesla's
            # /live_status payload is the total site rating in watts so it
            # covers single- and multi-unit installs. Both charge and
            # discharge use the same ceiling.
            nameplate_w = None
            if self._site_info_cache:
                nameplate_w = self._site_info_cache.get("nameplate_power")
            nameplate_kw = round(nameplate_w / 1000.0, 2) if nameplate_w else None

            # Total pack energy (nameplate Wh) and energy_left (stored Wh) come
            # from live_status when Tesla supplies them. When live_status omits
            # pack capacity, prefer the BMS-scanned Battery Health capacity over
            # the static battery_count × per-unit nameplate fallback.
            total_pack_kwh: float | None = None
            tpe_w = live_status.get("total_pack_energy")
            if tpe_w is not None:
                try:
                    total_pack_kwh = round(float(tpe_w) / 1000.0, 2)
                except (TypeError, ValueError):
                    total_pack_kwh = None
            if total_pack_kwh is None:
                total_pack_kwh = _stored_battery_health_capacity_kwh(
                    self.hass,
                    self._entry_id,
                )
            if total_pack_kwh is None and self._site_info_cache:
                # Last-resort fallback when no BMS scan has populated live
                # capacity yet.
                count = (
                    (self._site_info_cache.get("components") or {}).get("battery_count")
                    or self._site_info_cache.get("battery_count")
                )
                if count:
                    try:
                        total_pack_kwh = round(int(count) * 13.5, 2)
                    except (TypeError, ValueError):
                        pass

            soc_pct = self._resolve_battery_level_pct(live_status)
            energy_left_kwh: float | None = None
            el_w = live_status.get("energy_left")
            if el_w is not None:
                try:
                    energy_left_kwh = round(float(el_w) / 1000.0, 2)
                except (TypeError, ValueError):
                    energy_left_kwh = None
            if energy_left_kwh is None and total_pack_kwh is not None and soc_pct is not None:
                energy_left_kwh = round(total_pack_kwh * (soc_pct / 100.0), 2)

            # Backup time remaining (hours): stored kWh / current home load.
            # Caps at 999 to keep the UI sane when load drops near zero.
            backup_hours: float | None = None
            if energy_left_kwh is not None and load_kw and load_kw > 0.05:
                backup_hours = round(min(999.0, energy_left_kwh / load_kw), 1)

            # Grid services / VPP — present in live_status when site is enrolled.
            # When the site has no VPP the field is typically absent or 0;
            # default the power reading to 0 so the sensor reads a real value
            # ("0 W") rather than "Unknown" — much more useful for graphs.
            grid_services_active = bool(live_status.get("grid_services_active", False))
            grid_services_power_kw: float = 0.0
            gsp = live_status.get("grid_services_power")
            if gsp is not None:
                try:
                    grid_services_power_kw = round(float(gsp) / 1000.0, 3)
                except (TypeError, ValueError):
                    grid_services_power_kw = 0.0

            energy_data = {
                "solar_power": solar_kw,
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "battery_level": soc_pct,
                "grid_status": grid_status,
                "ev_power": ev_power_kw,
                "last_update": dt_util.utcnow(),
                "energy_summary": self._energy_acc.as_dict(),
                "firmware": self._firmware,
                # BMS ceiling for the mobile force-mode picker's Max chip
                "battery_max_charge_power": nameplate_kw,
                "battery_max_discharge_power": nameplate_kw,
                "battery_max_charge_power_w": nameplate_w,
                "battery_max_discharge_power_w": nameplate_w,
                # Powerwall extended fields
                "total_pack_energy_kwh": total_pack_kwh,
                "energy_left_kwh": energy_left_kwh,
                "backup_time_remaining_hours": backup_hours,
                "grid_services_active": grid_services_active,
                "grid_services_power_kw": grid_services_power_kw,
                "lifetime_totals": self._lifetime_totals,
            }

            # Refresh lifetime totals once per hour (best-effort, never fails the poll)
            _lifetime_stale = (time.monotonic() - self._lifetime_last_fetch) > 3600
            if _lifetime_stale and not self._lifetime_fetch_failed:
                try:
                    await self.async_refresh_lifetime_totals()
                    energy_data["lifetime_totals"] = self._lifetime_totals
                except Exception as err:
                    _LOGGER.debug("Lifetime totals refresh failed: %s", err)

            # Tesla API recovered — send recovery notification if we were in outage
            if self._outage_notified:
                outage_mins = int((time.monotonic() - self._outage_start) / 60)
                _LOGGER.warning(
                    "Tesla API recovered after %d min outage (site %s)",
                    outage_mins, self.site_id,
                )
                try:
                    from .automations.actions import _send_expo_push
                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Recovered",
                        f"Tesla API is back online after {outage_mins} min outage",
                    )
                except Exception:
                    pass
            self._consecutive_failures = 0
            self._failure_streak_start = 0
            self._outage_notified = False

            return energy_data

        except ConfigEntryAuthFailed:
            # Don't retry — let HA's reauth flow take over
            raise
        except (UpdateFailed, Exception) as err:
            now = time.monotonic()
            should_notify, failure_duration = self._record_tesla_update_failure(now)

            # Notify only after a sustained failure window. Refreshes can be
            # requested faster than the normal update interval, so attempt
            # count alone can report a short Tesla empty-response burst as a
            # server outage.
            if should_notify:
                self._outage_notified = True
                self._outage_start = self._failure_streak_start
                self._last_outage_notification = now
                _LOGGER.error(
                    "Tesla server outage detected: %d consecutive failures over %.0fs (site %s)",
                    self._consecutive_failures, failure_duration, self.site_id,
                )
                try:
                    from .automations.actions import _send_expo_push
                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Outage",
                        f"Tesla API unreachable — optimization paused. Error: {err}",
                    )
                except Exception:
                    pass
            elif self._outage_notified and (now - self._last_outage_notification) > 1800:
                # Repeat notification every 30 min during ongoing outage
                outage_mins = int((now - self._outage_start) / 60)
                self._last_outage_notification = now
                try:
                    from .automations.actions import _send_expo_push
                    await _send_expo_push(
                        self.hass,
                        "Tesla Server Outage",
                        f"Tesla API still unreachable after {outage_mins} min",
                    )
                except Exception:
                    pass

            if isinstance(err, UpdateFailed):
                raise
            raise UpdateFailed(f"Unexpected error fetching Tesla energy data: {err}") from err

    async def async_get_site_info(
        self,
        max_age: float | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch site_info from Tesla API (Teslemetry or Fleet API).

        Includes installation_time_zone which is critical for correct TOU schedule alignment.
        Results are cached since site info (especially timezone) doesn't change.

        Returns:
            Site info dict containing installation_time_zone, or None if fetch fails
        """
        cache_ttl = (
            TESLA_SITE_INFO_CACHE_TTL_SECONDS
            if max_age is None
            else max(0, float(max_age))
        )

        # Return cached value if still fresh.
        if (
            self._site_info_cache
            and (time.monotonic() - self._site_info_last_fetch) <= cache_ttl
        ):
            _LOGGER.debug("Returning cached site_info")
            return self._site_info_cache

        # Don't retry if a previous fetch already failed (avoids spamming logs every sync cycle)
        if self._site_info_fetch_failed:
            return None

        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            _LOGGER.info(f"Fetching site_info for site {self.site_id}")

            data = await _fetch_with_retry(
                self.session,
                f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/site_info",
                headers,
                max_retries=3,
                timeout_seconds=60,
                raise_auth_failed=self.api_provider != TESLA_PROVIDER_FLEET_API,
            )

            site_info = data.get("response", {})

            # Log timezone info for debugging
            installation_tz = site_info.get("installation_time_zone")
            if installation_tz:
                _LOGGER.info(f"Found Powerwall timezone: {installation_tz}")
            else:
                _LOGGER.warning("No installation_time_zone in site_info response")

            # Log battery capacity info for debugging
            _LOGGER.debug(f"Site info keys: {list(site_info.keys())}")
            components = site_info.get("components", {})
            if components:
                _LOGGER.debug(f"Site info components keys: {list(components.keys())}")
                # Log battery-related fields
                battery_fields = {k: v for k, v in site_info.items()
                                 if 'battery' in k.lower() or 'pack' in k.lower() or 'energy' in k.lower() or 'power' in k.lower()}
                if battery_fields:
                    _LOGGER.debug(f"Site info battery fields: {battery_fields}")
                component_battery = {k: v for k, v in components.items()
                                    if 'battery' in k.lower() or 'nameplate' in k.lower()}
                if component_battery:
                    _LOGGER.debug(f"Components battery fields: {component_battery}")

            # Extract firmware version
            gateways = components.get("gateways", []) or site_info.get("gateways", [])
            if gateways:
                gateway = gateways[0]
                _LOGGER.info("Gateway keys: %s", list(gateway.keys()))
                fw_version = (
                    gateway.get("firmware_version")
                    or gateway.get("version")
                    or gateway.get("gateway_firmware_version")
                    or gateway.get("fw_version")
                    or ""
                )
                if fw_version:
                    self._firmware = fw_version
                    _LOGGER.info("Firmware version: %s", fw_version)
                else:
                    _LOGGER.info("No firmware key found in gateway: %s", gateway)

            # Extract country (used for region-gating; Tesla reports ISO country code
            # in site_info for Energy Sites, though the key has varied historically).
            self._site_country = (
                site_info.get("country")
                or site_info.get("installation_country")
                or components.get("country")
            )

            # Opportunistically capture current state for new energy-site controls.
            # Tesla returns these in site_info when available; otherwise we fall back
            # to explicit GET calls during the capability probe.
            if "off_grid_vehicle_charging_reserve_percent" in site_info:
                self._off_grid_reserve_percent = site_info.get(
                    "off_grid_vehicle_charging_reserve_percent"
                )
            elif "off_grid_vehicle_charging_reserve_percent" in components:
                self._off_grid_reserve_percent = components.get(
                    "off_grid_vehicle_charging_reserve_percent"
                )

            storm_mode_active = (
                site_info.get("storm_mode_active")
                if "storm_mode_active" in site_info
                else components.get("storm_mode_active")
            )
            storm_mode_enabled = (
                site_info.get("user_settings", {}).get("storm_mode_enabled")
                if isinstance(site_info.get("user_settings"), dict)
                else None
            )
            if storm_mode_enabled is not None:
                self._storm_mode_enabled = bool(storm_mode_enabled)
            elif storm_mode_active is not None:
                self._storm_mode_enabled = bool(storm_mode_active)

            # Cache the result with timestamp
            self._site_info_cache = site_info
            self._site_info_last_fetch = time.monotonic()

            # Schedule one-shot capability probe on first successful fetch.
            # Runs in background to avoid blocking the main fetch path.
            if not self._capabilities_probed:
                self._capabilities_probed = True
                self.hass.async_create_task(
                    self._async_probe_tesla_capabilities(),
                    name=f"{DOMAIN}_tesla_capability_probe",
                )

            return site_info

        except UpdateFailed as err:
            _LOGGER.warning("Failed to fetch site_info: %s (will not retry until next restart)", err)
            self._site_info_fetch_failed = True
            return None
        except Exception as err:
            _LOGGER.warning("Unexpected error fetching site_info: %s (will not retry until next restart)", err)
            self._site_info_fetch_failed = True
            return None

    def invalidate_site_info_cache(self) -> None:
        """Force the next async_get_site_info() call to re-fetch from Tesla.

        Call this after any write that modifies site_info-level fields
        (backup reserve, operation mode, grid export rule, grid charging,
        storm mode, off-grid EV reserve, VPP enrollment) so that HA
        entities reading from the cache don't display stale values for
        up to six hours until the next natural refresh.
        """
        # Clear the cached payload itself, not just the timestamp.
        # async_get_site_info() returns cached data while it is inside the
        # caller's max_age window. Resetting only _site_info_last_fetch can
        # still leave a shorter-uptime HA instance inside that window, so clear
        # the cached payload itself to force the next call to refetch.
        self._site_info_cache = None
        self._site_info_last_fetch = 0
        self._site_info_fetch_failed = False
        _LOGGER.debug("Tesla site_info cache invalidated — next read will refetch")

    async def set_grid_charging_enabled(self, enabled: bool) -> bool:
        """
        Enable or disable grid charging (imports) for the Powerwall.

        Args:
            enabled: True to allow grid charging, False to disallow

        Returns:
            bool: True if successful, False otherwise
        """
        # Note: The API field is inverted - True means charging is DISALLOWED
        disallow_value = not enabled

        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            _LOGGER.info(f"Setting grid charging {'enabled' if enabled else 'disabled'} for site {self.site_id}")

            url = f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/grid_import_export"
            payload = {
                "disallow_charge_from_grid_with_solar_installed": disallow_value
            }

            async with self.session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status not in [200, 201, 202]:
                    text = await response.text()
                    _LOGGER.error(f"Failed to set grid charging: {response.status} - {text}")
                    return False

                data = await response.json()
                _LOGGER.debug(f"Set grid charging response: {data}")

                # Check for actual success in response body
                response_data = data.get("response", data)
                if isinstance(response_data, dict) and "result" in response_data:
                    if not response_data["result"]:
                        reason = response_data.get("reason", "Unknown reason")
                        _LOGGER.error(f"Set grid charging failed: {reason}")
                        return False

                _LOGGER.info(f"✅ Grid charging {'enabled' if enabled else 'disabled'} successfully for site {self.site_id}")
                self.invalidate_site_info_cache()
                return True

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout setting grid charging")
            return False
        except Exception as err:
            _LOGGER.error(f"Error setting grid charging: {err}")
            return False

    # ------------------------------------------------------------------
    # Unified Tesla Energy Site API helper
    # ------------------------------------------------------------------

    def _tesla_headers(self) -> dict[str, str]:
        """Build authorization headers using the freshest token."""
        return {
            "Authorization": f"Bearer {self._get_current_token()}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

    async def _tesla_api_call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        max_retries: int = 3,
        timeout_seconds: int = 30,
    ) -> tuple[int, dict | None]:
        """Make a Tesla Energy Site API call with retry/backoff.

        Returns (status_code, response_json_or_none). Retries on 429/5xx using
        Retry-After if provided, otherwise exponential backoff. Does NOT raise
        on 4xx — callers interpret status codes (e.g. probe uses 4xx to detect
        unsupported features).
        """
        url = f"{self.api_base_url}{path}"
        last_status = 0
        retry_after_delay: float | None = None

        for attempt in range(max_retries):
            try:
                if attempt > 0:
                    wait_time = retry_after_delay or (2 ** attempt)
                    retry_after_delay = None
                    await asyncio.sleep(wait_time)

                headers = self._tesla_headers()
                request = self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body if method.upper() != "GET" else None,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                )
                async with request as response:
                    last_status = response.status
                    if response.status == 200:
                        try:
                            return response.status, await response.json()
                        except Exception:
                            return response.status, None

                    if response.status in (429, 500, 502, 503, 504):
                        retry_after_delay = _parse_retry_after(response)
                        _LOGGER.warning(
                            "Tesla %s %s attempt %d/%d: %s",
                            method, path, attempt + 1, max_retries, response.status,
                        )
                        continue

                    # Non-retryable status — return as-is for caller inspection
                    try:
                        return response.status, await response.json()
                    except Exception:
                        return response.status, None

            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Tesla %s %s attempt %d/%d timed out",
                    method, path, attempt + 1, max_retries,
                )
                continue
            except aiohttp.ClientError as err:
                _LOGGER.warning(
                    "Tesla %s %s attempt %d/%d network error: %s",
                    method, path, attempt + 1, max_retries, err,
                )
                continue

        return last_status or 0, None

    # ------------------------------------------------------------------
    # Capability probe (run once after first site_info fetch)
    # ------------------------------------------------------------------

    async def _async_probe_tesla_capabilities(self) -> None:
        """Probe Tesla Energy Site endpoints to determine which features are supported.

        Tesla does not expose clean feature flags; instead we attempt a harmless
        GET on each new endpoint and interpret the response:
          - 200: feature supported → True
          - 404 / 501 / 400 "not_supported": unsupported → False
          - other 4xx: unknown (assume supported so user can retry)
          - 5xx / network error: unknown (assume supported; probe again later)
        Results are cached in self.tesla_capabilities and persist until restart.
        """
        _LOGGER.info("Probing Tesla Energy Site capabilities for site %s", self.site_id)

        async def _probe(name: str, path: str) -> bool:
            status, _body = await self._tesla_api_call("GET", path, max_retries=1, timeout_seconds=15)
            if status == 200:
                _LOGGER.info("Tesla capability '%s' supported (200)", name)
                return True
            if status in (400, 404, 405, 501):
                _LOGGER.info("Tesla capability '%s' unsupported (%d)", name, status)
                return False
            _LOGGER.info(
                "Tesla capability '%s' probe inconclusive (%d) — assuming supported",
                name, status,
            )
            return True

        # Run probes sequentially to be gentle on Tesla rate limits.
        base = f"/api/1/energy_sites/{self.site_id}"
        self.tesla_capabilities["storm_mode"] = await _probe(
            "storm_mode", f"{base}/storm_mode",
        )
        self.tesla_capabilities["off_grid_vehicle_charging_reserve"] = await _probe(
            "off_grid_vehicle_charging_reserve",
            f"{base}/off_grid_vehicle_charging_reserve",
        )
        # VPP programs endpoint returns the list of programs the site is eligible for.
        # An empty list still means the endpoint is supported (just no programs).
        status, body = await self._tesla_api_call(
            "GET", f"{base}/programs", max_retries=1, timeout_seconds=15,
        )
        if status == 200:
            programs = []
            if isinstance(body, dict):
                resp = body.get("response", body)
                if isinstance(resp, dict):
                    programs = resp.get("programs") or resp.get("enrolled_programs") or []
                elif isinstance(resp, list):
                    programs = resp
            self._vpp_programs_cache = programs if isinstance(programs, list) else []
            self.tesla_capabilities["vpp_programs"] = True
            _LOGGER.info(
                "Tesla capability 'vpp_programs' supported — %d programs available",
                len(self._vpp_programs_cache),
            )
        elif status in (400, 404, 405, 501):
            self.tesla_capabilities["vpp_programs"] = False
            _LOGGER.info("Tesla capability 'vpp_programs' unsupported (%d)", status)
        else:
            self.tesla_capabilities["vpp_programs"] = True
            _LOGGER.info(
                "Tesla capability 'vpp_programs' probe inconclusive (%d) — assuming supported",
                status,
            )

        # Notify platforms so entities can be (re)created now that capabilities are known.
        # The probe can complete before async_setup_entry publishes its full
        # hass.data entry, so create the per-entry dict instead of writing to a
        # throwaway default.
        entry_data = self.hass.data.setdefault(DOMAIN, {}).setdefault(self._entry_id, {})
        entry_data["tesla_capabilities"] = dict(self.tesla_capabilities)
        entry_data["tesla_site_country"] = self._site_country

        # Prune orphaned entities from prior sessions where a capability was
        # supported at the time but is no longer. Without this, the entity
        # registry keeps stale unique_ids which HA displays as "unavailable"
        # and the dashboard strategy will surface them as broken controls.
        self._cleanup_unsupported_tesla_entities()

    def _cleanup_unsupported_tesla_entities(self) -> None:
        """Remove registry entries for Tesla capabilities that the current
        site does not support. Called after every capability probe so that
        upgrading from a version where a capability was incorrectly detected
        (or switching sites) cleans up the orphans automatically."""
        try:
            from homeassistant.helpers import entity_registry as er
        except Exception:
            return
        try:
            ent_reg = er.async_get(self.hass)
        except Exception:
            return

        removed = 0

        def _remove_by_unique_id(domain: str, unique_id: str) -> None:
            nonlocal removed
            eid = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
            if eid:
                try:
                    ent_reg.async_remove(eid)
                    removed += 1
                    _LOGGER.debug("Removed orphaned Tesla entity %s", eid)
                except Exception as err:
                    _LOGGER.debug("Failed to remove %s: %s", eid, err)

        if self.tesla_capabilities.get("storm_mode") is False:
            _remove_by_unique_id("switch", f"{self._entry_id}_tesla_storm_watch")
            _remove_by_unique_id("binary_sensor", f"{self._entry_id}_tesla_storm_watch_active")

        if self.tesla_capabilities.get("off_grid_vehicle_charging_reserve") is False:
            _remove_by_unique_id("number", f"{self._entry_id}_tesla_off_grid_ev_reserve")

        if self.tesla_capabilities.get("vpp_programs") is False:
            # Remove every vpp_* switch created under this entry
            try:
                for reg_entry in list(ent_reg.entities.values()):
                    if (reg_entry.config_entry_id == self._entry_id
                        and reg_entry.domain == "switch"
                        and reg_entry.platform == DOMAIN
                        and "_tesla_vpp_" in (reg_entry.unique_id or "")):
                        ent_reg.async_remove(reg_entry.entity_id)
                        removed += 1
                        _LOGGER.debug("Removed orphaned VPP switch %s", reg_entry.entity_id)
            except Exception as err:
                _LOGGER.debug("Failed to scan VPP switches: %s", err)

        if removed > 0:
            _LOGGER.info(
                "Cleaned up %d orphaned Tesla capability entities (site no longer supports them)",
                removed,
            )

    # ------------------------------------------------------------------
    # New Energy Site controls (storm mode, off-grid EV reserve, VPP programs)
    # ------------------------------------------------------------------

    async def async_set_storm_watch(self, enabled: bool) -> bool:
        """Enable or disable Tesla Storm Watch (predictive pre-charging)."""
        path = f"/api/1/energy_sites/{self.site_id}/storm_mode"
        status, _body = await self._tesla_api_call(
            "POST", path, json_body={"enabled": bool(enabled)},
        )
        if status == 200:
            self._storm_mode_enabled = bool(enabled)
            self.invalidate_site_info_cache()
            _LOGGER.info("Storm Watch %s for site %s", "enabled" if enabled else "disabled", self.site_id)
            return True
        _LOGGER.error("Failed to set storm mode for site %s: HTTP %s", self.site_id, status)
        return False

    async def async_get_storm_watch_status(self) -> dict | None:
        """Fetch current storm watch enabled + active state."""
        path = f"/api/1/energy_sites/{self.site_id}/storm_mode"
        status, body = await self._tesla_api_call("GET", path)
        if status != 200 or not isinstance(body, dict):
            return None
        resp = body.get("response", body)
        if not isinstance(resp, dict):
            return None
        if "enabled" in resp:
            self._storm_mode_enabled = bool(resp.get("enabled"))
        return resp

    async def async_set_off_grid_ev_reserve(self, percent: int) -> bool:
        """Set off-grid vehicle charging reserve percent (0-100)."""
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            _LOGGER.error("Invalid off-grid EV reserve value: %r", percent)
            return False
        percent = max(0, min(100, percent))
        path = f"/api/1/energy_sites/{self.site_id}/off_grid_vehicle_charging_reserve"
        status, _body = await self._tesla_api_call(
            "POST", path, json_body={"off_grid_vehicle_charging_reserve_percent": percent},
        )
        if status == 200:
            self._off_grid_reserve_percent = percent
            self.invalidate_site_info_cache()
            _LOGGER.info("Off-grid EV reserve set to %d%% for site %s", percent, self.site_id)
            return True
        _LOGGER.error("Failed to set off-grid EV reserve for site %s: HTTP %s", self.site_id, status)
        return False

    async def async_get_vpp_programs(self, force_refresh: bool = False) -> list[dict]:
        """Fetch VPP / grid-services programs the site is eligible for.

        Each program is a dict; Tesla's schema has varied but typically includes
        ``id`` / ``program_id``, ``name``, and an ``enrolled`` / ``is_enrolled``
        flag.
        """
        if self._vpp_programs_cache is not None and not force_refresh:
            return self._vpp_programs_cache
        path = f"/api/1/energy_sites/{self.site_id}/programs"
        status, body = await self._tesla_api_call("GET", path)
        if status != 200 or not isinstance(body, dict):
            return self._vpp_programs_cache or []
        resp = body.get("response", body)
        programs: list[dict] = []
        if isinstance(resp, dict):
            raw = resp.get("programs") or resp.get("enrolled_programs") or []
            if isinstance(raw, list):
                programs = [p for p in raw if isinstance(p, dict)]
        elif isinstance(resp, list):
            programs = [p for p in resp if isinstance(p, dict)]
        self._vpp_programs_cache = programs
        return programs

    async def async_set_vpp_enrollment(self, program_id: str, enrolled: bool) -> bool:
        """Opt in or out of a Tesla VPP / grid-services program."""
        if not program_id:
            _LOGGER.error("Missing program_id for VPP enrollment")
            return False
        path = f"/api/1/energy_sites/{self.site_id}/programs"
        payload = {
            "program_id": program_id,
            "enrolled": bool(enrolled),
        }
        status, _body = await self._tesla_api_call("POST", path, json_body=payload)
        if status == 200:
            # Invalidate caches so next reads pick up new state.
            self._vpp_programs_cache = None
            self.invalidate_site_info_cache()
            _LOGGER.info(
                "VPP program %s %s for site %s",
                program_id, "enrolled" if enrolled else "unenrolled", self.site_id,
            )
            return True
        _LOGGER.error(
            "Failed to set VPP enrollment for site %s program %s: HTTP %s",
            self.site_id, program_id, status,
        )
        return False

    async def async_get_calendar_history(
        self,
        period: str = "day",
        kind: str = "energy",
        end_date: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Fetch calendar history from Tesla API.

        Args:
            period: 'day', 'week', 'month', 'year', or 'lifetime'
            kind: 'energy' or 'power'
            end_date: Optional end date in YYYY-MM-DD format (defaults to today)

        Returns:
            Calendar history data with time_series array, or None if fetch fails
        """
        current_token = self._get_current_token()
        headers = {
            "Authorization": f"Bearer {current_token}",
            "Content-Type": "application/json",
            "User-Agent": POWER_SYNC_USER_AGENT,
        }

        try:
            # Get site timezone from site_info
            site_info = await self.async_get_site_info()
            timezone = "Australia/Brisbane"  # Default fallback
            if site_info:
                timezone = site_info.get("installation_time_zone", timezone)

            # Calculate end_date in site's timezone
            from zoneinfo import ZoneInfo
            from datetime import timedelta
            user_tz = ZoneInfo(timezone)

            # Use provided end_date or default to now
            if end_date:
                try:
                    reference_date = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=user_tz)
                except ValueError:
                    reference_date = datetime.now(user_tz)
            else:
                reference_date = datetime.now(user_tz)

            end_dt = reference_date.replace(hour=23, minute=59, second=59)
            end_date_iso = end_dt.isoformat()

            _LOGGER.info(f"Fetching calendar history for site {self.site_id}: period={period}, kind={kind}, end_date={end_date}")

            params = {
                "kind": kind,
                "period": period,
                "end_date": end_date_iso,
                "time_zone": timezone,
            }

            url = f"{self.api_base_url}/api/1/energy_sites/{self.site_id}/calendar_history"

            async with self.session.get(
                url,
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    _LOGGER.error(f"Failed to fetch calendar history: {response.status} - {text}")
                    return None

                data = await response.json()
                result = data.get("response", {})
                time_series = result.get("time_series", [])

                _LOGGER.info(f"Fetched {len(time_series)} raw records from Tesla for period='{period}'")

                # Tesla API often returns all historical data regardless of period
                # Filter client-side based on requested period and end_date
                if time_series and period in ["day", "week", "month", "year"]:
                    # Calculate cutoff date based on period, relative to reference_date
                    if period == "day":
                        cutoff = reference_date.replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "week":
                        cutoff = (reference_date - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "month":
                        cutoff = (reference_date - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
                    elif period == "year":
                        cutoff = (reference_date - timedelta(days=365)).replace(hour=0, minute=0, second=0, microsecond=0)

                    # End of reference day as upper bound
                    end_of_day = reference_date.replace(hour=23, minute=59, second=59, microsecond=999999)

                    filtered_series = []
                    for entry in time_series:
                        try:
                            ts_str = entry.get("timestamp", "")
                            if ts_str:
                                entry_dt = datetime.fromisoformat(ts_str)
                                if cutoff <= entry_dt <= end_of_day:
                                    filtered_series.append(entry)
                        except (ValueError, TypeError) as e:
                            _LOGGER.warning(f"Failed to parse timestamp: {entry.get('timestamp')}: {e}")
                            continue

                    _LOGGER.info(f"Filtered calendar history from {len(time_series)} to {len(filtered_series)} records for period='{period}' (cutoff={cutoff.date()}, end={end_of_day.date()})")
                    time_series = filtered_series

                _LOGGER.info(f"Successfully fetched calendar history: {len(time_series)} records for period='{period}'")

                return {
                    "period": period,
                    "time_series": time_series,
                    "serial_number": result.get("serial_number"),
                    "installation_date": result.get("installation_date"),
                }

        except asyncio.TimeoutError:
            _LOGGER.error("Timeout fetching calendar history")
            return None
        except Exception as err:
            _LOGGER.error(f"Error fetching calendar history: {err}")
            return None

    async def async_refresh_lifetime_totals(self) -> dict[str, float] | None:
        """Sum calendar_history period=lifetime into a small dict of kWh totals.

        Tesla returns Wh per bucket (yearly bins from install date). Result is
        cached in ``self._lifetime_totals`` so sensors return the last good value
        between refreshes; on permanent failure (e.g. unsupported endpoint),
        ``_lifetime_fetch_failed`` short-circuits subsequent calls.
        """
        history = await self.async_get_calendar_history(period="lifetime")
        if not history:
            return self._lifetime_totals

        totals = {key: 0.0 for key in LIFETIME_TOTAL_KEYS}
        for ts in history.get("time_series", []) or []:
            totals["lifetime_solar_kwh"] += (ts.get("solar_energy_exported") or 0)
            totals["lifetime_grid_import_kwh"] += (ts.get("grid_energy_imported") or 0)
            totals["lifetime_grid_export_kwh"] += (
                (ts.get("grid_energy_exported_from_solar") or 0)
                + (ts.get("grid_energy_exported_from_battery") or 0)
            )
            totals["lifetime_battery_charged_kwh"] += (
                (ts.get("battery_energy_imported_from_grid") or 0)
                + (ts.get("battery_energy_imported_from_solar") or 0)
            )
            totals["lifetime_battery_discharged_kwh"] += (ts.get("battery_energy_exported") or 0)
            totals["lifetime_home_kwh"] += (
                (ts.get("consumer_energy_imported_from_grid") or 0)
                + (ts.get("consumer_energy_imported_from_solar") or 0)
                + (ts.get("consumer_energy_imported_from_battery") or 0)
            )

        # Tesla returns Wh; convert to kWh
        for k in totals:
            totals[k] = round(totals[k] / 1000.0, 3)

        totals = self._clamp_lifetime_totals(totals)
        self._lifetime_totals = totals
        self._lifetime_last_fetch = time.monotonic()
        await self.async_flush_lifetime_totals()
        return totals


class DemandChargeCoordinator(DataUpdateCoordinator):
    """Coordinator to track demand charges."""

    def __init__(
        self,
        hass: HomeAssistant,
        energy_coordinator: DataUpdateCoordinator,
        enabled: bool = False,
        rate: float = 0.0,
        start_time: str = "14:00",
        end_time: str = "20:00",
        days: str = "All Days",
        billing_day: int = 1,
        daily_supply_charge: float = 0.0,
        monthly_supply_charge: float = 0.0,
    ) -> None:
        """Initialize the coordinator."""
        self.tesla_coordinator = energy_coordinator
        self.enabled = enabled
        self.rate = rate
        self.start_time = start_time
        self.end_time = end_time
        self.days = days
        self.billing_day = billing_day
        self.daily_supply_charge = daily_supply_charge
        self.monthly_supply_charge = monthly_supply_charge

        # Track peak demand (persists across coordinator updates)
        self._peak_demand_kw = 0.0
        self._last_billing_day_check = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_demand_charge",
            update_interval=timedelta(minutes=1),  # Check every minute
        )

    def _is_in_peak_period(self, now: datetime) -> bool:
        """Check if current time is within peak period and correct day."""
        try:
            # Check if today matches the configured days filter
            weekday = now.weekday()  # 0=Monday, 6=Sunday
            if self.days == "Weekdays Only" and weekday >= 5:
                return False  # Saturday or Sunday
            elif self.days == "Weekends Only" and weekday < 5:
                return False  # Monday through Friday

            # Check if current time is within peak period
            # Handle both "HH:MM" and "HH:MM:SS" formats
            start_parts = self.start_time.split(":")
            start_hour, start_minute = int(start_parts[0]), int(start_parts[1])
            end_parts = self.end_time.split(":")
            end_hour, end_minute = int(end_parts[0]), int(end_parts[1])

            current_minutes = now.hour * 60 + now.minute
            start_minutes = start_hour * 60 + start_minute
            end_minutes = end_hour * 60 + end_minute

            # Handle overnight periods (e.g., 22:00 to 06:00)
            if end_minutes <= start_minutes:
                # Peak period wraps around midnight
                return current_minutes >= start_minutes or current_minutes < end_minutes
            else:
                # Normal daytime peak period
                return start_minutes <= current_minutes < end_minutes

        except (ValueError, AttributeError) as err:
            _LOGGER.error("Invalid time format for demand charge period: %s", err)
            return False

    async def _async_update_data(self) -> dict[str, Any]:
        """Update demand charge tracking data."""
        if not self.enabled:
            return {
                "in_peak_period": False,
                "grid_import_power_kw": 0.0,
                "peak_demand_kw": 0.0,
                "estimated_cost": 0.0,
            }

        # Check for billing cycle reset
        now = dt_util.now()
        current_day = now.day

        # If we've crossed the billing day, reset peak demand
        if self._last_billing_day_check is not None:
            # Check if we've passed the billing day since last check
            last_check_day = self._last_billing_day_check.day
            if current_day == self.billing_day and last_check_day != self.billing_day:
                _LOGGER.info("Billing cycle reset triggered on day %d", self.billing_day)
                self.reset_peak_demand()

        self._last_billing_day_check = now

        # Get current grid power from energy coordinator (Tesla, FoxESS, Sigenergy, or Sungrow)
        energy_data = self.tesla_coordinator.data or {}
        grid_power_kw = energy_data.get("grid_power", 0.0)

        # Grid import is positive, export is negative
        # We only care about import for demand charges
        grid_import_kw = max(0, grid_power_kw)

        # Check if in peak period
        in_peak_period = self._is_in_peak_period(now)

        # Update peak demand only for samples inside the billable demand window.
        if in_peak_period and grid_import_kw > self._peak_demand_kw:
            self._peak_demand_kw = grid_import_kw
            _LOGGER.info("New peak demand: %.2f kW", self._peak_demand_kw)

        # Calculate estimated demand charge cost (peak demand * rate)
        estimated_demand_cost = self._peak_demand_kw * self.rate

        # Calculate days elapsed in current billing cycle
        days_elapsed = self._calculate_days_elapsed(now)

        # Calculate days until next billing cycle reset
        days_until_reset = self._calculate_days_until_reset(now)

        # Calculate daily supply charge cost (accumulates daily)
        daily_supply_cost = self.daily_supply_charge * days_elapsed

        # Calculate total monthly cost
        total_monthly_cost = estimated_demand_cost + daily_supply_cost + self.monthly_supply_charge

        return {
            "in_peak_period": in_peak_period,
            "grid_import_power_kw": grid_import_kw,
            "peak_demand_kw": self._peak_demand_kw,
            "estimated_cost": estimated_demand_cost,
            "daily_supply_charge_cost": daily_supply_cost,
            "monthly_supply_charge": self.monthly_supply_charge,
            "total_monthly_cost": total_monthly_cost,
            "days_until_reset": days_until_reset,
            "last_update": dt_util.utcnow(),
        }

    def reset_peak_demand(self) -> None:
        """Reset peak demand tracking (e.g., at start of new billing cycle)."""
        _LOGGER.info("Resetting peak demand from %.2f kW to 0", self._peak_demand_kw)
        self._peak_demand_kw = 0.0

    def _calculate_days_elapsed(self, now: datetime) -> int:
        """Calculate days elapsed since last billing day."""
        current_day = now.day

        if current_day >= self.billing_day:
            # We're past the billing day this month
            days_elapsed = current_day - self.billing_day + 1
        else:
            # We haven't reached the billing day this month yet
            # Need to count from last month's billing day
            # Get the last day of previous month
            first_of_this_month = now.replace(day=1)
            last_month = first_of_this_month - timedelta(days=1)
            last_day_of_last_month = last_month.day

            # Days from billing day last month to end of last month
            if self.billing_day <= last_day_of_last_month:
                days_in_last_month = last_day_of_last_month - self.billing_day + 1
            else:
                # Billing day doesn't exist in last month (e.g., Feb 30)
                # Start from last day of last month
                days_in_last_month = 1

            # Plus days in current month
            days_elapsed = days_in_last_month + current_day

        return days_elapsed

    def _calculate_days_until_reset(self, now: datetime) -> int:
        """Calculate days until next billing cycle reset."""
        current_day = now.day

        if current_day < self.billing_day:
            # Next reset is this month
            return self.billing_day - current_day
        else:
            # Next reset is next month
            # Get the last day of this month
            if now.month == 12:
                next_month = now.replace(year=now.year + 1, month=1, day=1)
            else:
                next_month = now.replace(month=now.month + 1, day=1)

            last_day_this_month = (next_month - timedelta(days=1)).day

            # Days remaining in this month plus billing day in next month
            days_remaining_this_month = last_day_this_month - current_day
            return days_remaining_this_month + self.billing_day


class AEMOPriceCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches AEMO price data directly from AEMO API.

    This coordinator provides an alternative to AmberPriceCoordinator for users
    who want to use AEMO wholesale pricing without an Amber subscription.

    Fetches data directly from AEMO NEMWeb - no external integration required.
    The data is converted to Amber-compatible format so the existing tariff
    converter can be reused.

    Uses adaptive polling to catch new dispatch files quickly:
      WAIT       (>10 s until boundary)  -> 45 s intervals, skip NEMWEB fetch
      PRE-ACTIVE (-10 s ... +15 s)       -> 5 s intervals, fetch NEMWEB
      ACTIVE     (>15 s past boundary)   -> 1 s intervals, fetch NEMWEB
    """

    # Adaptive polling thresholds (seconds relative to the next 5-minute boundary)
    _WAIT_INTERVAL = 45       # Poll interval while well away from the boundary (s)
    _PRE_ACTIVE_WINDOW = 10   # Start gentle polling this many seconds before boundary
    _PRE_ACTIVE_INTERVAL = 5  # Poll interval in the pre-active window (s)
    _ACTIVE_WINDOW = 15       # Switch to rapid polling this many seconds after boundary
    _ACTIVE_INTERVAL = 1      # Poll interval during active file search (s)

    def __init__(
        self,
        hass: HomeAssistant,
        region: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            region: NEM region code (NSW1, QLD1, VIC1, SA1, TAS1)
            session: aiohttp client session for API requests
        """
        from .aemo_api import AEMOAPIClient

        self.region = region
        self._client = AEMOAPIClient(session)

        # Adaptive polling state
        self._next_boundary: datetime | None = None
        self._polling_mode: str = "active"  # Start active to get first data fast

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_aemo",
            # Start with 1s interval; adaptive logic will adjust after first data
            update_interval=timedelta(seconds=self._ACTIVE_INTERVAL),
        )

    # ------------------------------------------------------------------
    # Adaptive polling helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_aemo_timestamp(timestamp_str: str) -> datetime | None:
        """Parse AEMO dispatch timestamp (always AEST UTC+10) to naive local datetime."""
        if not timestamp_str or "/" not in timestamp_str:
            return None
        try:
            from datetime import timezone as _tz, timedelta as _td
            aest = _tz(_td(hours=10))
            dt_naive = datetime.strptime(timestamp_str, "%Y/%m/%d %H:%M:%S")
            dt_aest = dt_naive.replace(tzinfo=aest)
            return dt_aest.astimezone().replace(tzinfo=None)
        except (ValueError, TypeError) as e:
            _LOGGER.debug("Failed to parse dispatch timestamp '%s': %s", timestamp_str, e)
            return None

    @staticmethod
    def _calc_next_boundary() -> datetime:
        """Return the next 5-minute wall-clock boundary from now (naive local)."""
        now = datetime.now()
        next_min = ((now.minute // 5) + 1) * 5
        if next_min >= 60:
            return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return now.replace(minute=next_min, second=0, microsecond=0)

    def _adjust_poll_interval(self) -> bool:
        """Set update_interval based on proximity to the next dispatch boundary.

        Returns True when we should actually hit NEMWEB this cycle, False when
        we should serve cached data and wait for the boundary.
        """
        if self._next_boundary is None:
            # No boundary known yet - poll now to get first data
            return True

        now = datetime.now()
        secs = (self._next_boundary - now).total_seconds()

        # Mode-transition logs are demoted to DEBUG: each one fires once per
        # 5-min period and the wording ("ACTIVE mode (1 s intervals) -
        # searching for new dispatch file") read as alarming to users with
        # debug logging enabled even though the underlying poll is just a
        # cheap directory listing on AEMO's public NEMWEB. The actual
        # dispatch arrival is still logged at INFO ("AEMO: New dispatch -
        # next boundary X" / "NEMWEB dispatch: ... -> N regions") which is
        # the line that matters for users debugging tariff sync.
        if secs > self._PRE_ACTIVE_WINDOW:
            # WAIT mode - too early to expect a new file
            if self._polling_mode != "wait":
                self._polling_mode = "wait"
                _LOGGER.debug(
                    "AEMO: WAIT mode - next boundary %s in %ds",
                    self._next_boundary.strftime("%H:%M:%S"),
                    int(secs),
                )
            self.update_interval = timedelta(seconds=self._WAIT_INTERVAL)
            return False

        if secs > -self._ACTIVE_WINDOW:
            # PRE-ACTIVE mode - gently start checking
            if self._polling_mode != "pre-active":
                self._polling_mode = "pre-active"
                _LOGGER.debug("AEMO: PRE-ACTIVE mode (5 s intervals)")
            self.update_interval = timedelta(seconds=self._PRE_ACTIVE_INTERVAL)
            return True

        # ACTIVE mode - new file could appear any second
        if self._polling_mode != "active":
            self._polling_mode = "active"
            _LOGGER.debug("AEMO: ACTIVE mode (1 s intervals)")
        self.update_interval = timedelta(seconds=self._ACTIVE_INTERVAL)
        return True

    # ------------------------------------------------------------------
    # Main update loop
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from AEMO API using adaptive polling.

        Polling strategy:
        - After receiving a new dispatch file: enter WAIT mode until just
          before the next 5-minute boundary (45 s check interval).
        - 10 s before the boundary: switch to PRE-ACTIVE (5 s interval).
        - 15 s after the boundary: switch to ACTIVE (1 s interval) and poll
          NEMWEB aggressively until a new file appears.
        - On new file: immediately return to WAIT mode.

        Returns:
            dict with 'current', 'forecast', and 'last_update' in Amber-compatible format
        """
        # Decide whether to hit NEMWEB this cycle
        should_fetch = self._adjust_poll_interval()

        if not should_fetch:
            # WAIT mode - return existing data unchanged
            if self.data:
                return self.data
            # No data yet - fall through to fetch
            should_fetch = True

        try:
            # Fetch current price (5-min dispatch price) with file metadata
            current_prices_all, is_new_dispatch, dispatch_file = (
                await self._client.get_current_prices_with_file()
            )

            current_price_data = None
            if current_prices_all:
                current_price_data = current_prices_all.get(self.region)

            # Handle adaptive boundary tracking
            if is_new_dispatch and current_price_data:
                timestamp = current_price_data.get("timestamp")
                if timestamp:
                    period_dt = self._parse_aemo_timestamp(timestamp)
                    if period_dt:
                        self._next_boundary = self._calc_next_boundary()
                        _LOGGER.info(
                            "AEMO: New dispatch - next boundary %s",
                            self._next_boundary.strftime("%H:%M:%S"),
                        )
            elif not is_new_dispatch and self._next_boundary is None and current_price_data:
                # First run - file already cached but we still need a boundary
                timestamp = current_price_data.get("timestamp")
                if timestamp:
                    period_dt = self._parse_aemo_timestamp(timestamp)
                    if period_dt:
                        candidate = self._calc_next_boundary()
                        secs_until = (candidate - datetime.now()).total_seconds()
                        if secs_until > -self._ACTIVE_WINDOW:
                            self._next_boundary = candidate
                            _LOGGER.info(
                                "AEMO: Boundary initialised from cached dispatch: "
                                "next=%s (in %.0fs)",
                                self._next_boundary.strftime("%H:%M:%S"),
                                secs_until,
                            )

            # Only fetch forecast when we got a new dispatch file (predispatch
            # updates every ~30 min, no point hammering it every second in ACTIVE)
            forecast = None
            if is_new_dispatch:
                forecast = await self._client.get_price_forecast(self.region, periods=96)

            # If no new forecast, preserve existing
            if not forecast and self.data:
                forecast = self.data.get("forecast")

            if not forecast:
                raise UpdateFailed(f"Failed to fetch AEMO forecast for {self.region}")

            # Get current price - prefer current dispatch price, fall back to first forecast
            if current_price_data:
                # Convert $/MWh to c/kWh: $/MWh / 10 = c/kWh
                current_price_cents = current_price_data["price"] / 10.0
                price_source = "dispatch"
            else:
                # Fall back to first forecast period
                current_price_cents = forecast[0]["perKwh"] if forecast else 0
                price_source = "forecast"
                _LOGGER.warning("Could not get current AEMO price, using forecast")

            # Create current price in Amber format
            current_prices = [
                {
                    "perKwh": current_price_cents,
                    "channelType": "general",
                    "type": "CurrentInterval",
                },
                {
                    "perKwh": -current_price_cents,
                    "channelType": "feedIn",
                    "type": "CurrentInterval",
                },
            ]

            if is_new_dispatch:
                _LOGGER.info(
                    "AEMO API data for %s: current=%.2fc/kWh (%s), forecast_periods=%d",
                    self.region, current_price_cents, price_source, len(forecast) // 2
                )
                async_dispatcher_send(
                    self.hass,
                    SIGNAL_AEMO_NEW_DISPATCH,
                    {
                        "region": self.region,
                        "file": dispatch_file,
                        "price_cents": current_price_cents,
                    },
                )

            return {
                "current": current_prices,
                "forecast": forecast,
                "last_update": dt_util.utcnow(),
                "source": "aemo_api",
                "dispatch_file": dispatch_file,
            }

        except Exception as err:
            raise UpdateFailed(f"Error fetching AEMO data: {err}") from err


# Keep old name as alias for backwards compatibility
AEMOSensorCoordinator = AEMOPriceCoordinator


class FlowPowerKWatchPriceCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches Flow Power KWatch API prices."""

    def __init__(
        self,
        hass: HomeAssistant,
        region: str,
        api_key: str,
        session: aiohttp.ClientSession,
    ) -> None:
        from .flow_power_api import FlowPowerAPIClient

        self.region = region
        self.api_region = FLOW_POWER_KWATCH_REGIONS.get(region, region.lower())
        self._client = FlowPowerAPIClient(api_key, session)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_flow_power_kwatch",
            update_interval=timedelta(minutes=5),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch current and forecast prices from Flow Power's KWatch API."""
        from .flow_power_api import kwatch_prices_to_amber_format

        try:
            dispatch = await self._client.dispatch5mins(self.api_region, period=60)
            # Keep the first upcoming half-hour slot; period=2 skips it.
            forecast_30 = await self._client.predispatch30mins(self.api_region, period=1)
            forecast_5 = await self._client.predispatch5mins(self.api_region, period=60)

            if not dispatch:
                raise UpdateFailed(f"No KWatch dispatch prices returned for {self.region}")

            latest_dispatch = dispatch[-1:]
            current_prices = kwatch_prices_to_amber_format(
                latest_dispatch,
                interval_type="CurrentInterval",
                default_duration=5,
            )
            forecast = kwatch_prices_to_amber_format(
                forecast_30,
                interval_type="ForecastInterval",
                default_duration=30,
            )
            forecast_5min = kwatch_prices_to_amber_format(
                forecast_5 or dispatch,
                interval_type="ForecastInterval",
                default_duration=5,
            )

            if not forecast:
                forecast = forecast_5min
            if not forecast:
                raise UpdateFailed(f"No KWatch forecast prices returned for {self.region}")

            latest_cents = latest_dispatch[0]["perKwh"]
            _LOGGER.info(
                "Flow Power KWatch data for %s: current=%.2fc/kWh, forecast_periods=%d",
                self.region,
                latest_cents,
                len(forecast) // 2,
            )

            return {
                "current": current_prices,
                "forecast": forecast,
                "forecast_5min": forecast_5min,
                "last_update": dt_util.utcnow(),
                "source": "flow_power_kwatch",
            }
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching Flow Power KWatch data: {err}") from err


class EPEXPriceCoordinator(DataUpdateCoordinator):
    """Coordinator that fetches EPEX day-ahead price data.

    Uses the EPEX Predictor API (epexpredictor.batzill.com) for European
    day-ahead electricity prices. Supports DE, AT, BE, NL, SE1-4, DK1-2.

    The API applies surcharges and taxes server-side, so returned prices
    are the final consumer price in ct/kWh.

    Data is converted to Amber-compatible format for the optimizer.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        region: str,
        session: aiohttp.ClientSession,
        surcharge: float = 0.0,
        tax_percent: float = 0.0,
        export_rate: float = 0.0,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            region: EPEX bidding zone code (DE, AT, BE, NL, SE1-4, DK1-2)
            session: aiohttp client session for API requests
            surcharge: Fixed surcharge in ct/kWh (network fees, levies)
            tax_percent: Tax percentage (e.g. 21 for Belgian VAT)
            export_rate: Fixed feed-in rate in ct/kWh (0 = use wholesale price)
        """
        from .epex_api import EPEXAPIClient

        self.region = region
        self._surcharge = surcharge
        self._tax_percent = tax_percent
        self._export_rate = export_rate
        self._client = EPEXAPIClient(session)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_epex",
            update_interval=timedelta(minutes=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from EPEX API and convert to Amber-compatible format.

        Returns:
            dict with 'current', 'forecast', and 'last_update' in Amber-compatible format
        """
        try:
            prices = await self._client.get_prices(
                region=self.region,
                surcharge=self._surcharge,
                tax_percent=self._tax_percent,
            )

            if not prices:
                raise UpdateFailed(f"No prices returned from EPEX API for {self.region}")

            now = dt_util.utcnow()
            current_prices = []
            forecast_prices = []

            for entry in prices:
                starts_at_str = entry.get("startsAt", "")
                total_ct = entry.get("total", 0)

                if not starts_at_str:
                    continue

                try:
                    starts_at = datetime.fromisoformat(starts_at_str)
                    if starts_at.tzinfo is None:
                        starts_at = starts_at.replace(tzinfo=dt_util.UTC)
                    ends_at = starts_at + timedelta(hours=1)
                except (ValueError, TypeError):
                    continue

                # Determine interval type
                if starts_at <= now < ends_at:
                    interval_type = "CurrentInterval"
                elif ends_at <= now:
                    interval_type = "ActualInterval"
                else:
                    interval_type = "ForecastInterval"

                # Import price entry (ct/kWh = Amber's perKwh format)
                import_entry = {
                    "nemTime": ends_at.isoformat(),
                    "perKwh": total_ct,
                    "channelType": "general",
                    "type": interval_type,
                    "duration": 60,
                }

                # Export price: use fixed rate if configured, otherwise wholesale (no surcharge/tax)
                if self._export_rate > 0:
                    export_ct = -self._export_rate
                else:
                    # Use negative of import price (wholesale approximation)
                    export_ct = -total_ct

                export_entry = {
                    "nemTime": ends_at.isoformat(),
                    "perKwh": export_ct,
                    "channelType": "feedIn",
                    "type": interval_type,
                    "duration": 60,
                }

                if interval_type == "CurrentInterval":
                    current_prices.extend([import_entry, export_entry])
                elif interval_type == "ForecastInterval":
                    forecast_prices.extend([import_entry, export_entry])

            if not current_prices and forecast_prices:
                # No current interval yet — use first forecast as current
                current_prices = forecast_prices[:2]

            _LOGGER.info(
                "EPEX API data for %s: %d current, %d forecast entries "
                "(surcharge=%.1f ct, tax=%.1f%%)",
                self.region,
                len(current_prices),
                len(forecast_prices),
                self._surcharge,
                self._tax_percent,
            )

            return {
                "current": current_prices,
                "forecast": forecast_prices,
                "last_update": dt_util.utcnow(),
                "source": "epex_api",
            }

        except Exception as err:
            raise UpdateFailed(f"Error fetching EPEX data: {err}") from err


class SigenergyEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Sigenergy energy data via Modbus.

    Polls the Sigenergy inverter system via Modbus TCP to get real-time
    power data (solar, battery, grid, load) and battery state of charge.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        entry_id: str = "",
        max_export_limit_kw: Optional[float] = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            host: IP address of Sigenergy system
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            entry_id: Config entry ID for price lookups
            max_export_limit_kw: User-configured DNSP export limit in kW
        """
        from .inverters.sigenergy import SigenergyController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._entry_id = entry_id
        self._controller = SigenergyController(host, port, slave_id, max_export_limit_kw=max_export_limit_kw)
        self._energy_acc = EnergyAccumulator(hass, "sigenergy")
        # Rated charge/discharge power in kW — cached after first successful
        # read from input registers 30079/30081. Static hardware spec so it
        # only needs to be fetched once.
        self._rated_charge_power_kw: Optional[float] = None
        self._rated_discharge_power_kw: Optional[float] = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sigenergy_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    async def _async_read_evdc_charger_state(self):
        """Read EVDC charger state when the configured charger is DC-side."""
        try:
            entry = self.hass.config_entries.async_get_entry(self._entry_id)
        except Exception:
            entry = None
        if not entry:
            return None

        opts = {**entry.data, **entry.options}
        if not opts.get(CONF_SIGENERGY_CHARGER_ENABLED):
            return None
        charger_type = str(
            opts.get(CONF_SIGENERGY_CHARGER_TYPE, SIGENERGY_CHARGER_EVAC)
        ).lower()
        if charger_type != SIGENERGY_CHARGER_EVDC:
            return None

        from .sigenergy_charger_config import resolve_sigenergy_charger_connection

        config = resolve_sigenergy_charger_connection(
            entry,
            hass=self.hass,
            fallback_host=self.host,
        )
        host = str(config["host"]).strip()
        if not host:
            return None

        from .sigenergy_charger import SigenergyEVChargerController

        controller = SigenergyEVChargerController(
            host=host,
            port=config["port"],
            slave_id=config["slave_id"],
            charger_type=SIGENERGY_CHARGER_EVDC,
        )
        try:
            return await controller.read_state()
        except Exception as err:
            _LOGGER.debug("Sigenergy EVDC state read failed during energy update: %s", err)
            return None
        finally:
            await controller.disconnect()

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Sigenergy system via Modbus."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        try:
            status = await self._controller.get_status()

            attrs = status.attributes or {}

            # If Modbus returned no battery data, keep previous readings
            # rather than reporting SOC=0% which causes optimizer issues.
            if "battery_soc" not in attrs:
                if self.data:
                    _LOGGER.warning(
                        "Sigenergy Modbus returned no battery data — keeping previous readings"
                    )
                    return self.data
                raise UpdateFailed("Sigenergy Modbus connection failed — no data available")

            # Map Sigenergy data to standard format (same as Tesla)
            # Power values in kW from Modbus, we keep them in kW for sensors
            dc_solar_kw = attrs.get("pv_power_kw", 0)
            ac_solar_kw = attrs.get("third_party_pv_power_kw", 0)  # AC-coupled via Smart Port
            solar_kw = dc_solar_kw + ac_solar_kw
            grid_kw = attrs.get("grid_power_kw", 0)  # Positive = importing, negative = exporting

            # Sigenergy battery sign convention is OPPOSITE to Tesla:
            # Sigenergy Modbus: Positive = charging (into battery), Negative = discharging (out of battery)
            # Tesla/PowerSync: Positive = discharging (out of battery), Negative = charging (into battery)
            # So we negate the value to match Tesla convention
            battery_kw_raw = attrs.get("battery_power_kw", 0)
            battery_kw = -battery_kw_raw  # Flip sign to match Tesla convention

            evdc_state = await self._async_read_evdc_charger_state()
            evdc_power_kw = (
                evdc_state.power_kw
                if evdc_state and evdc_state.power_kw is not None
                else 0.0
            )

            # Balance-derived Sigenergy load includes DC-side EVDC power. Keep
            # home load separate so EVDC charging/discharge is modeled as an EV
            # branch rather than household demand.
            load_kw = sigenergy_home_load_kw(
                solar_kw=solar_kw,
                grid_kw=grid_kw,
                battery_kw=battery_kw,
                evdc_power_kw=evdc_power_kw,
            )

            # Accumulate daily energy from power readings (with cost tracking)
            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

            # Rated charge/discharge power — hardware spec, static. Fetch once
            # from the ESS rated power registers via the controller's internal
            # read path, then cache for the lifetime of the coordinator.
            if self._rated_charge_power_kw is None or self._rated_discharge_power_kw is None:
                try:
                    rc_regs = await self._controller._read_input_registers(
                        self._controller.REG_ESS_RATED_CHARGE_POWER, 2
                    )
                    rd_regs = await self._controller._read_input_registers(
                        self._controller.REG_ESS_RATED_DISCHARGE_POWER, 2
                    )
                    if rc_regs and len(rc_regs) >= 2:
                        raw = self._controller._to_unsigned32(rc_regs[0], rc_regs[1])
                        if 0 < raw < 0xFFFFFFFE:
                            self._rated_charge_power_kw = raw / 1000.0
                    if rd_regs and len(rd_regs) >= 2:
                        raw = self._controller._to_unsigned32(rd_regs[0], rd_regs[1])
                        if 0 < raw < 0xFFFFFFFE:
                            self._rated_discharge_power_kw = raw / 1000.0
                except Exception as e:
                    _LOGGER.debug("Sigenergy rated power read failed (will retry): %s", e)

            energy_data = {
                "solar_power": solar_kw,  # kW (DC + AC-coupled)
                "grid_power": grid_kw,  # kW, positive = importing, negative = exporting
                "battery_power": battery_kw,  # kW, positive = discharging, negative = charging
                "load_power": load_kw,  # kW, calculated from energy balance
                "ev_power": evdc_power_kw,  # kW, positive = EV charging, negative = V2X discharge
                "ev_power_kw": evdc_power_kw,
                "ev_charger_type": evdc_state.charger_type if evdc_state else None,
                "ev_charger_status": evdc_state.status if evdc_state else None,
                "ev_charger_connected": evdc_state.is_connected if evdc_state else False,
                "ev_charger_charging": evdc_state.is_charging if evdc_state else False,
                "ev_charger_discharging": evdc_state.is_discharging if evdc_state else False,
                "ev_soc": evdc_state.vehicle_soc if evdc_state else None,
                "battery_level": attrs.get("battery_soc", 0),  # %
                "last_update": dt_util.utcnow(),
                # Extra Sigenergy-specific data
                "active_power_kw": attrs.get("active_power_kw", 0),
                "export_limit_kw": attrs.get("export_limit_kw"),
                "ems_work_mode": attrs.get("ems_work_mode"),
                "is_curtailed": status.is_curtailed,
                "third_party_pv_power_kw": ac_solar_kw,  # AC-coupled solar via Smart Port
                # Battery health data
                "battery_soh": attrs.get("battery_soh"),  # % State of Health
                "battery_capacity_kwh": attrs.get("battery_capacity_kwh"),  # kWh rated capacity
                # Rated BMS power for the mobile force-mode picker's "Max" chip
                "battery_max_charge_power": self._rated_charge_power_kw,
                "battery_max_discharge_power": self._rated_discharge_power_kw,
                "battery_max_charge_power_w": (
                    int(self._rated_charge_power_kw * 1000)
                    if self._rated_charge_power_kw else None
                ),
                "battery_max_discharge_power_w": (
                    int(self._rated_discharge_power_kw * 1000)
                    if self._rated_discharge_power_kw else None
                ),
                "energy_summary": self._energy_acc.as_dict(),
            }

            _LOGGER.debug(
                "Sigenergy data: solar=%.2f kW (dc=%.2f, ac=%.2f), grid=%.2f kW, battery=%.2f kW (%.0f%%), evdc=%.2f kW, load=%.2f kW, curtailed=%s",
                energy_data["solar_power"],
                dc_solar_kw,
                ac_solar_kw,
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["ev_power"],
                energy_data["load_power"],
                energy_data["is_curtailed"],
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching Sigenergy energy data: {err}") from err

    async def set_backup_mode(self) -> bool:
        """Set Sigenergy to STANDBY for IDLE (prevents all charge/discharge)."""
        async with self._controller:
            return await self._controller.set_standby_mode()

    async def set_no_discharge_mode(self) -> bool:
        """Block Sigenergy battery discharge while still allowing battery charge."""
        async with self._controller:
            mode_ok = await self._controller.set_self_consumption_mode()
            limit_ok = await self._controller.set_discharge_rate_limit(0)
            return bool(mode_ok and limit_ok)

    async def restore_no_discharge_mode(self) -> bool:
        """Restore Sigenergy discharge capacity after no-discharge preserve mode."""
        async with self._controller:
            return await self._controller.restore_normal()

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore self-consumption mode after IDLE."""
        async with self._controller:
            return await self._controller.restore_from_standby()

    async def async_shutdown(self) -> None:
        """Disconnect from Sigenergy system on shutdown."""
        await self._controller.disconnect()


class AlphaESSEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch AlphaESS energy data via Modbus (primary) with
    optional AlphaESS Cloud API fallback.

    AlphaESS hybrid inverter-battery systems (SMILE / Storion) expose a rich
    Modbus TCP register map (slave ID 0x55 by default). Cloud is used only
    when Modbus is unreachable.

    Sign conventions (unlike Sigenergy):
      - Battery power (reg 0126H): NEGATIVE = charging, POSITIVE = discharging
        → already matches PowerSync convention, no flip needed.
      - Grid power (reg 0021H): POSITIVE = importing, NEGATIVE = exporting
        (standard grid-meter convention).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 85,
        entry_id: str = "",
        max_export_limit_kw: Optional[float] = None,
        cloud_client: Optional[Any] = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance.
            host: IP address of AlphaESS inverter.
            port: Modbus TCP port (default 502).
            slave_id: Modbus slave ID (default 85 = 0x55).
            entry_id: Config entry ID for price lookups.
            max_export_limit_kw: User-configured DNSP export safety cap.
            cloud_client: Optional AlphaESSCloudClient for telemetry fallback.
        """
        from .inverters.alphaess import AlphaESSController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._entry_id = entry_id
        self._controller = AlphaESSController(
            host, port, slave_id, max_export_limit_kw=max_export_limit_kw
        )
        self._energy_acc = EnergyAccumulator(hass, "alphaess")
        self._cloud = cloud_client
        self._modbus_failures = 0  # Consecutive failures → cloud fallback

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_alphaess_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch AlphaESS data, preferring Modbus and falling back to cloud."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        attrs: dict[str, Any] = {}
        is_curtailed = False
        source = "modbus"

        try:
            status = await self._controller.get_status()
            attrs = status.attributes or {}
            is_curtailed = status.is_curtailed

            if "battery_soc" not in attrs:
                raise UpdateFailed("AlphaESS Modbus returned no battery data")

            self._modbus_failures = 0

        except Exception as modbus_err:
            self._modbus_failures += 1
            _LOGGER.warning(
                "AlphaESS Modbus read failed (%d consecutive): %s",
                self._modbus_failures,
                modbus_err,
            )

            # Try cloud fallback if configured
            if self._cloud is not None:
                try:
                    cloud_data = await self._cloud.get_last_power_data()
                    attrs = _normalize_alphaess_cloud_data(cloud_data)
                    source = "cloud"
                    _LOGGER.info("AlphaESS fell back to cloud telemetry")
                except Exception as cloud_err:
                    _LOGGER.error("AlphaESS cloud fallback also failed: %s", cloud_err)
                    if self.data:
                        return self.data
                    raise UpdateFailed(
                        f"AlphaESS Modbus and cloud both failed: "
                        f"modbus={modbus_err}; cloud={cloud_err}"
                    ) from modbus_err
            else:
                if self.data:
                    return self.data
                raise UpdateFailed(f"AlphaESS Modbus failed: {modbus_err}") from modbus_err

        solar_kw = attrs.get("pv_power_kw", 0) or 0
        grid_kw = attrs.get("grid_power_kw", 0) or 0  # + import, − export
        # AlphaESS battery sign already matches PowerSync: + = discharge, − = charge
        battery_kw = attrs.get("battery_power_kw", 0) or 0

        # Load from balance: solar + grid + battery (with sign conventions above)
        load_kw = solar_kw + grid_kw + battery_kw

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

        # BMS-reported power limits (W) — used to default force-mode power and
        # to cap the mobile app slider so users can't request more than the
        # battery can deliver.
        max_charge_w = attrs.get("battery_max_charge_power_w")
        max_discharge_w = attrs.get("battery_max_discharge_power_w")

        energy_data = {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": attrs.get("battery_soc", 0),
            "battery_soh": attrs.get("battery_soh"),
            "battery_capacity_kwh": attrs.get("battery_capacity_kwh"),
            # Expose BMS limits in both W (raw) and kW (display-friendly)
            "battery_max_charge_power_w": max_charge_w,
            "battery_max_discharge_power_w": max_discharge_w,
            "battery_max_charge_power": (max_charge_w / 1000.0) if max_charge_w else None,
            "battery_max_discharge_power": (max_discharge_w / 1000.0) if max_discharge_w else None,
            "export_limit_percent": attrs.get("export_limit_percent"),
            "is_curtailed": is_curtailed,
            "work_mode_raw": attrs.get("work_mode_raw"),
            "data_source": source,
            "last_update": dt_util.utcnow(),
            "energy_summary": self._energy_acc.as_dict(),
        }

        _LOGGER.debug(
            "AlphaESS (%s): solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.1f%%), "
            "load=%.2f kW, curtailed=%s",
            source,
            energy_data["solar_power"],
            energy_data["grid_power"],
            energy_data["battery_power"],
            energy_data["battery_level"],
            energy_data["load_power"],
            energy_data["is_curtailed"],
        )
        return energy_data

    async def set_backup_mode(self) -> bool:
        """IDLE hold — release dispatch but write zero-power dispatch if needed."""
        async with self._controller:
            return await self._controller.set_standby_mode()

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore self-consumption after IDLE hold."""
        async with self._controller:
            return await self._controller.restore_from_standby()

    # Safety floor when no BMS reading is available (e.g. first poll hasn't
    # completed). SMILE5 rated power, well inside every supported model's
    # BMS limit. The controller further clamps against 0x012C/0x012D.
    _DEFAULT_FORCE_POWER_W = 5000.0

    def _resolve_force_power_w(self, requested_w: float, direction: str) -> float:
        """Pick the force-mode power to actually write.

        - If the caller passed a positive value, use it (controller clamps to BMS max).
        - Otherwise, read the last BMS-reported max from self.data
          (battery_max_charge_power_w / battery_max_discharge_power_w).
        - If the BMS value isn't available yet, fall back to _DEFAULT_FORCE_POWER_W.

        Args:
            requested_w: Power from the caller (mobile app / service call).
            direction: "charge" or "discharge" — selects which BMS field to read.
        """
        if requested_w and requested_w > 0:
            return float(requested_w)

        field = (
            "battery_max_charge_power_w"
            if direction == "charge"
            else "battery_max_discharge_power_w"
        )
        bms_w = (self.data or {}).get(field)
        if bms_w and bms_w > 0:
            _LOGGER.info(
                "AlphaESS: caller passed power_w<=0, auto-defaulting to BMS %s max = %.0f W",
                direction, bms_w,
            )
            return float(bms_w)

        _LOGGER.warning(
            "AlphaESS: no BMS %s power reading available yet — using safety default %.0f W",
            direction, self._DEFAULT_FORCE_POWER_W,
        )
        return self._DEFAULT_FORCE_POWER_W

    async def force_charge(self, duration_min: int = 30, power_w: float = 0.0) -> bool:
        """Force-charge the battery via the Note29 dispatch block.

        Args:
            duration_min: Force-mode duration in minutes. Passed down to Para6
                as seconds — the inverter auto-stops when the timer elapses.
                HA also runs its own expiry timer as a belt-and-braces fallback.
            power_w: Charge power in watts (positive). 0 or negative falls back
                to the BMS-reported max charge power, then to a 5 kW safety
                default if the BMS reading isn't available yet.
        """
        power_w = self._resolve_force_power_w(power_w, "charge")
        duration_seconds = max(60, int(duration_min) * 60)
        _LOGGER.info(
            "AlphaESS coordinator: force_charge(power_w=%.0f, duration=%dm/%ds)",
            power_w, duration_min, duration_seconds,
        )
        async with self._controller:
            return await self._controller.force_charge(
                power_kw=power_w / 1000.0,
                duration_seconds=duration_seconds,
            )

    async def force_discharge(self, duration_min: int = 30, power_w: float = 0.0) -> bool:
        """Force-discharge the battery via the Note29 dispatch block.

        Same fallback chain as force_charge — see its docstring.
        """
        power_w = self._resolve_force_power_w(power_w, "discharge")
        duration_seconds = max(60, int(duration_min) * 60)
        _LOGGER.info(
            "AlphaESS coordinator: force_discharge(power_w=%.0f, duration=%dm/%ds)",
            power_w, duration_min, duration_seconds,
        )
        async with self._controller:
            return await self._controller.force_discharge(
                power_kw=power_w / 1000.0,
                duration_seconds=duration_seconds,
            )

    async def restore_normal(self) -> bool:
        """Release dispatch and restore export limit to normal."""
        _LOGGER.info("AlphaESS coordinator: restore_normal")
        async with self._controller:
            return await self._controller.restore_normal()

    async def async_shutdown(self) -> None:
        """Release dispatch and disconnect on shutdown.

        AlphaESS has no auto-revert: if we leave 0722H=1, the battery stays
        locked in forced mode. We must release dispatch before dropping the
        connection (disconnect itself is intentionally pure — see the
        controller's disconnect() docstring for why).
        """
        try:
            await self._controller.release_dispatch()
        except Exception as e:
            _LOGGER.warning("AlphaESS release_dispatch on shutdown failed: %s", e)
        await self._controller.disconnect()
        if self._cloud is not None:
            try:
                await self._cloud.close()
            except Exception:
                pass


def _normalize_alphaess_cloud_data(cloud_data: dict) -> dict:
    """Translate AlphaESS cloud getLastPowerData response to Modbus-shaped attrs.

    Cloud fields (per AlphaESS Open API):
      - ppv:   PV power (W, positive)
      - pgrid: grid power (W, + import)
      - pbat:  battery power (W) — cloud convention has been observed as
               + discharge / − charge (same as Modbus 0126H); kept without flip.
      - soc:   battery state of charge (%)
    """
    attrs: dict[str, Any] = {}
    if not isinstance(cloud_data, dict):
        return attrs

    ppv = cloud_data.get("ppv")
    if isinstance(ppv, (int, float)):
        attrs["pv_power_w"] = ppv
        attrs["pv_power_kw"] = round(ppv / 1000.0, 3)

    pgrid = cloud_data.get("pgrid")
    if isinstance(pgrid, (int, float)):
        attrs["grid_power_w"] = pgrid
        attrs["grid_power_kw"] = round(pgrid / 1000.0, 3)

    pbat = cloud_data.get("pbat")
    if isinstance(pbat, (int, float)):
        attrs["battery_power_w"] = pbat
        attrs["battery_power_kw"] = round(pbat / 1000.0, 3)

    soc = cloud_data.get("soc")
    if isinstance(soc, (int, float)):
        attrs["battery_soc"] = round(float(soc), 1)

    return attrs


class SungrowEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Sungrow SH-series battery system data via Modbus.

    Polls the Sungrow hybrid inverter via Modbus TCP to get real-time
    power data (solar, battery, grid, load), battery SOC/SOH, and control settings.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        entry_id: str = "",
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            host: IP address of Sungrow inverter
            port: Modbus TCP port (default: 502)
            slave_id: Modbus slave ID (default: 1)
            entry_id: Config entry ID for price lookups
        """
        from .inverters.sungrow_sh import SungrowSHController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._entry_id = entry_id
        self._controller = SungrowSHController(host, port, slave_id)
        self._energy_acc = EnergyAccumulator(hass, "sungrow")
        # Sungrow/WiNet Modbus is sensitive to overlapping TCP operations.
        # Keep each coordinator poll or control command as one serialized
        # transaction so a refresh cannot close/reopen the shared client in the
        # middle of a force charge/discharge sequence.
        self._modbus_lock = asyncio.Lock()

        # Midnight baselines for computing daily import/export from total registers
        # Used when daily registers (13035/13044) read 0 (e.g. SH10RS + SBH)
        self._total_import_baseline: float | None = None
        self._total_export_baseline: float | None = None
        self._baseline_date: str | None = None  # ISO date string
        self._pre_control_charge_limit_kw: float | None = None
        self._pre_control_discharge_limit_kw: float | None = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sungrow_energy",
            update_interval=UPDATE_INTERVAL_ENERGY,
        )

    def _update_total_baselines(self, data: dict) -> None:
        """Track midnight baselines for total import/export registers.

        Some Sungrow systems (e.g. SH10RS + SBH) have no working daily
        import/export registers — they permanently read 0.  We derive
        daily values from the total (lifetime) registers by subtracting
        a baseline captured at midnight (or on first read of the day).
        """
        today = dt_util.now().date().isoformat()
        total_import = data.get("total_import")
        total_export = data.get("total_export")

        if self._baseline_date != today:
            # New day — capture baselines from current total values
            if total_import is not None:
                self._total_import_baseline = total_import
            if total_export is not None:
                self._total_export_baseline = total_export
            self._baseline_date = today
            _LOGGER.info(
                "Sungrow daily baseline reset: import=%.1f export=%.1f kWh (total)",
                self._total_import_baseline or 0, self._total_export_baseline or 0,
            )

    def _build_energy_summary(self, data: dict) -> dict:
        """Build energy summary using Sungrow register-based daily values.

        The inverter tracks daily energy counters in hardware, which are more
        reliable than the software accumulator (immune to transient bad reads
        from firmware that returns garbage for S32 power registers).

        Falls back to the accumulator for any values the registers don't provide
        (e.g. cost tracking).
        """
        summary = self._energy_acc.as_dict()

        # Override kWh counters with register-based values when available.
        # Some Sungrow systems have no external energy meter paired, so the
        # daily import/export registers (13035/13044) permanently read 0.
        # Detect this by checking whether the register reads 0 while the
        # software accumulator has already recorded energy — if so, try
        # deriving daily values from the total (lifetime) registers.
        daily_pv = data.get("daily_pv_generation")
        daily_import = data.get("daily_import")
        daily_export = data.get("daily_export")
        daily_discharge = data.get("daily_battery_discharge")
        daily_charge = data.get("daily_battery_charge")

        # Update midnight baselines for total register delta method
        self._update_total_baselines(data)

        if daily_pv is not None:
            summary["pv_today_kwh"] = daily_pv
        else:
            # No daily PV register (e.g. FoxESS) — use energy accumulator
            summary["pv_today_kwh"] = self._energy_acc.solar_kwh
        # For import/export: prefer daily register → total delta → accumulator
        if daily_import is not None and daily_import > 0:
            summary["grid_import_today_kwh"] = daily_import
        else:
            # Daily register missing or 0 — derive from total register delta
            total_import = data.get("total_import")
            if total_import is not None and self._total_import_baseline is not None:
                derived = round(total_import - self._total_import_baseline, 2)
                if derived >= 0:
                    summary["grid_import_today_kwh"] = derived
            # else: keep accumulator value (already in summary)

        if daily_export is not None and daily_export > 0:
            summary["grid_export_today_kwh"] = daily_export
        else:
            # Daily register missing or 0 — derive from total register delta
            total_export = data.get("total_export")
            if total_export is not None and self._total_export_baseline is not None:
                derived = round(total_export - self._total_export_baseline, 2)
                if derived >= 0:
                    summary["grid_export_today_kwh"] = derived
            # else: keep accumulator value (already in summary)
        if daily_discharge is not None:
            summary["discharge_today_kwh"] = daily_discharge
        if daily_charge is not None:
            summary["charge_today_kwh"] = daily_charge

        # Use the final (possibly corrected) import/export values for load calc
        final_import = summary.get("grid_import_today_kwh", 0)
        final_export = summary.get("grid_export_today_kwh", 0)

        # Calculate daily load from energy balance (no register for this)
        if all(v is not None for v in (daily_pv, daily_discharge, daily_charge)):
            summary["load_today_kwh"] = round(max(0,
                daily_pv + final_import + (daily_discharge or 0) - final_export - (daily_charge or 0)
            ), 2)

        # Recompute daily avg using possibly-overridden load from hardware registers
        load_kwh = summary.get("load_today_kwh", 0.0) or 0.0
        if load_kwh > 0:
            import_cost = summary.get("import_cost_today", 0.0) or 0.0
            export_earn = summary.get("export_earnings_today", 0.0) or 0.0
            summary["avg_cost_per_kwh_today"] = round((import_cost - export_earn) / load_kwh, 4)
        else:
            summary["avg_cost_per_kwh_today"] = None

        return summary

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Sungrow system via Modbus."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        try:
            async with self._modbus_lock:
                data = await self._controller.get_battery_data()

            # If Modbus returned no battery data, keep previous readings
            # rather than reporting SOC=0% which causes the optimizer to
            # incorrectly schedule IDLE (thinking the battery is empty).
            if "battery_soc" not in data:
                if self.data:
                    _LOGGER.warning(
                        "Sungrow Modbus returned no battery data — keeping previous readings"
                    )
                    return self.data
                raise UpdateFailed("Sungrow Modbus connection failed — no data available")

            # Map Sungrow data to standard format
            battery_power_w = data.get("battery_power", 0)  # Signed: positive = discharging
            export_power_w = data.get("export_power", 0)  # Signed: positive = exporting
            meter_power_w = data.get("meter_power")  # Signed: positive = importing, negative = exporting
            load_power_w = data.get("load_power")
            pv_power_w = data.get("pv_power")  # Direct PV DC power from register 5017-5018

            # Convert to kW for consistency with other coordinators
            battery_kw = battery_power_w / 1000
            if meter_power_w is not None:
                grid_kw = meter_power_w / 1000
            else:
                grid_kw = -export_power_w / 1000  # Invert: positive = importing, negative = exporting
            load_kw = (load_power_w or 0) / 1000

            # Use direct PV reading if available; otherwise calculate from energy balance
            if pv_power_w is not None:
                solar_kw = max(0, pv_power_w / 1000)
                # Derive load from energy balance: Load = Solar + Grid_Import + Battery_Discharge
                # (more reliable than the load register on some firmware)
                calc_load_kw = max(0.0, solar_kw + grid_kw + battery_kw)
                if abs(load_kw) > 100:
                    # Load register is garbage, use calculated value
                    load_kw = calc_load_kw
                elif load_power_w is None or (load_kw <= 0.01 and calc_load_kw > 0.05):
                    # Some Sungrow firmware reports the load register as 0 W
                    # while PV/grid/battery registers still describe real load.
                    load_kw = calc_load_kw
            else:
                # Fallback: estimate solar from energy balance
                solar_kw = max(0, load_kw - grid_kw - battery_kw)

            ac_inverter_kw = _configured_ac_inverter_power_kw(self.hass, self._entry_id)
            if ac_inverter_kw > 0:
                combined_load_kw = max(0.0, solar_kw + ac_inverter_kw + grid_kw + battery_kw)
                if combined_load_kw > load_kw:
                    load_kw = combined_load_kw

            # Accumulate daily energy from power readings (with cost tracking)
            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

            # Sanity-check SOC — 0xFFFF (6553.5%) means Modbus returned invalid data
            raw_soc = data.get("battery_soc", 0)
            if raw_soc > 100:
                _LOGGER.warning(
                    "Sungrow returned invalid SOC=%.1f%% (possible Modbus conflict). "
                    "Check for other integrations using port 502.",
                    raw_soc,
                )
                raw_soc = 0

            energy_data = {
                "solar_power": max(0, solar_kw),  # kW, clamp to 0 if calculated negative
                "grid_power": grid_kw,  # kW, positive = importing, negative = exporting
                "battery_power": battery_kw,  # kW, positive = discharging, negative = charging
                "load_power": load_kw,  # kW
                "battery_level": raw_soc,  # %
                "last_update": dt_util.utcnow(),
                # Sungrow-specific data
                "battery_soh": data.get("battery_soh"),  # % State of Health
                "battery_voltage": data.get("battery_voltage"),
                "battery_current": data.get("battery_current"),
                "battery_temp": data.get("battery_temp"),
                "inverter_temperature": data.get("inverter_temperature"),
                "ems_mode": data.get("ems_mode"),
                "ems_mode_name": data.get("ems_mode_name"),
                "charge_cmd": data.get("charge_cmd"),
                "min_soc": data.get("min_soc"),
                "max_soc": data.get("max_soc"),
                "backup_reserve": data.get("backup_reserve"),
                "charge_rate_limit_kw": data.get("charge_rate_limit_kw"),
                "discharge_rate_limit_kw": data.get("discharge_rate_limit_kw"),
                "export_limit_w": data.get("export_limit_w"),
                "export_limit_enabled": data.get("export_limit_enabled"),
                "meter_power": meter_power_w,
                "ac_inverter_solar_power": ac_inverter_kw,
                # Aliases for the mobile force-mode picker's Max chip.
                # The *_rate_limit_kw values already reflect BMS-reported
                # current × voltage, so reuse them rather than duplicate.
                "battery_max_charge_power": data.get("charge_rate_limit_kw"),
                "battery_max_discharge_power": data.get("discharge_rate_limit_kw"),
                "battery_max_charge_power_w": (
                    int(data["charge_rate_limit_kw"] * 1000)
                    if data.get("charge_rate_limit_kw") else None
                ),
                "battery_max_discharge_power_w": (
                    int(data["discharge_rate_limit_kw"] * 1000)
                    if data.get("discharge_rate_limit_kw") else None
                ),
                "energy_summary": self._build_energy_summary(data),
            }

            es = energy_data["energy_summary"]
            _LOGGER.debug(
                "Sungrow data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW | "
                "daily: pv=%.2f import=%.2f export=%.2f charge=%.2f discharge=%.2f load=%.2f kWh",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
                es.get("pv_today_kwh", 0),
                es.get("grid_import_today_kwh", 0),
                es.get("grid_export_today_kwh", 0),
                es.get("charge_today_kwh", 0),
                es.get("discharge_today_kwh", 0),
                es.get("load_today_kwh", 0),
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching Sungrow energy data: {err}") from err

    # Battery control methods - delegate to controller
    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set Sungrow to forced charge mode.

        Args:
            duration_minutes: Duration in minutes (not used by Sungrow - charge until manually stopped)
            power_w: Target forced charge power in watts.

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            target_power_w = power_w if power_w > 0 else 5000
            return await self._controller.force_charge(power_w=target_power_w)

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set Sungrow to forced discharge mode.

        Args:
            duration_minutes: Duration in minutes (not used by Sungrow - discharge until manually stopped)
            power_w: Target forced discharge power in watts.

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            target_power_w = power_w if power_w > 0 else 5000
            return await self._controller.force_discharge(power_w=target_power_w)

    async def force_grid_export(
        self,
        duration_minutes: int = 30,
        export_limit_w: float = 0,
    ) -> bool:
        """Force battery discharge while limiting grid export separately.

        Spread-export wants a target grid export rate, not a lower inverter
        discharge ceiling. Keep the battery discharge cap at the normal inverter
        limit so home load spikes can still be served by the battery, and use
        Sungrow's export-limit register to constrain export to grid.
        """
        async with self._modbus_lock, self._controller:
            target_export_w = max(0, int(round(export_limit_w or 0)))

            await self._capture_export_limit_for_restore()
            await self._capture_discharge_limit_for_restore()

            normal_limit_kw = await self._resolve_normal_discharge_limit_kw()
            if normal_limit_kw is None or normal_limit_kw <= 0:
                normal_limit_kw = max(target_export_w / 1000.0, 5.0)

            forced_power_w = int(round(normal_limit_kw * 1000))
            limit_changed = False
            export_limit_changed = False
            try:
                limit_changed = await self._controller.set_discharge_rate_limit(normal_limit_kw)
                if not limit_changed:
                    if getattr(self._controller, "rate_limit_writable", None) is False:
                        _LOGGER.warning(
                            "Sungrow spread export: discharge limit register is not writable; "
                            "continuing with grid export limit only"
                        )
                    else:
                        _LOGGER.warning(
                            "Sungrow spread export: failed to set discharge limit to %.2fkW",
                            normal_limit_kw,
                        )
                        return False

                export_limit_changed = await self._controller.set_export_limit(target_export_w)
                if not export_limit_changed:
                    _LOGGER.warning(
                        "Sungrow spread export: failed to set grid export limit to %dW",
                        target_export_w,
                    )
                    await self._restore_captured_discharge_limit()
                    return False

                result = await self._controller.force_discharge(power_w=forced_power_w)
            except Exception:
                if export_limit_changed:
                    await self._restore_captured_export_limit()
                if limit_changed:
                    await self._restore_captured_discharge_limit()
                raise

            if not result:
                await self._restore_captured_export_limit()
                await self._restore_captured_discharge_limit()

            return result

    async def restore_normal(self) -> bool:
        """Restore Sungrow to self-consumption mode.

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            normal_ok = await self._controller.restore_normal()
            export_limit_ok = await self._restore_captured_export_limit()
            charge_limit_ok = await self._restore_captured_charge_limit()
            limit_ok = await self._restore_captured_discharge_limit()
            return bool(normal_ok and export_limit_ok and charge_limit_ok and limit_ok)

    async def set_max_soc(self, percent: int) -> bool:
        """Set maximum battery SOC percentage.

        Args:
            percent: Maximum SOC percentage (0-100)

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            return await self._controller.set_max_soc(percent)

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve percentage.

        Args:
            percent: Backup reserve SOC percentage (0-100)

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            return await self._controller.set_backup_reserve(percent)

    async def set_backup_mode(self) -> bool:
        """Block Sungrow discharge for IDLE while still allowing battery charge."""
        async with self._modbus_lock, self._controller:
            await self._capture_discharge_limit_for_restore()
            limit_ok = await self._controller.set_discharge_rate_limit(0)
            if not limit_ok:
                # Some Sungrow firmware exposes 10 W as the minimum writable
                # discharge cap. Use that as a near-zero fallback.
                limit_ok = await self._controller.set_discharge_rate_limit(0.01)
            return bool(limit_ok)

    async def set_no_discharge_mode(self) -> bool:
        """Block Sungrow battery discharge while still allowing battery charge."""
        async with self._modbus_lock, self._controller:
            await self._capture_discharge_limit_for_restore()
            limit_ok = await self._controller.set_discharge_rate_limit(0)
            if not limit_ok:
                limit_ok = await self._controller.set_discharge_rate_limit(0.01)
            return bool(limit_ok)

    async def restore_no_discharge_mode(self) -> bool:
        """Restore Sungrow from scheduled EV no-discharge preserve mode."""
        async with self._modbus_lock, self._controller:
            normal_ok = await self._controller.restore_normal()
            limit_ok = await self._restore_captured_discharge_limit()
            return bool(normal_ok and limit_ok)

    async def _capture_discharge_limit_for_restore(self) -> None:
        """Save the normal Sungrow discharge limit before a temporary cap."""
        if getattr(self, "_pre_control_discharge_limit_kw", None) is not None:
            return

        try:
            current_limit_kw = await self._resolve_normal_discharge_limit_kw()
            if current_limit_kw is not None:
                self._pre_control_discharge_limit_kw = current_limit_kw
        except Exception as err:
            _LOGGER.debug(
                "Could not capture Sungrow discharge limit before temporary cap: %s",
                err,
            )

    async def _capture_export_limit_for_restore(self) -> None:
        """Save the current Sungrow export limit before a temporary target."""
        if getattr(self, "_pre_control_export_limit_captured", False):
            return

        export_limit_w: int | None = None
        export_limit_enabled: bool | None = None

        coord_data = getattr(self, "data", None) or {}
        if "export_limit_enabled" in coord_data:
            export_limit_enabled = bool(coord_data.get("export_limit_enabled"))
        if coord_data.get("export_limit_w") is not None:
            try:
                export_limit_w = int(float(coord_data.get("export_limit_w")))
            except (TypeError, ValueError):
                export_limit_w = None

        if export_limit_enabled is None or (export_limit_enabled and export_limit_w is None):
            try:
                live_data = await self._controller.get_battery_data()
            except Exception as err:
                _LOGGER.debug(
                    "Could not read live Sungrow export limit for restore target: %s",
                    err,
                )
            else:
                if "export_limit_enabled" in live_data:
                    export_limit_enabled = bool(live_data.get("export_limit_enabled"))
                if live_data.get("export_limit_w") is not None:
                    try:
                        export_limit_w = int(float(live_data.get("export_limit_w")))
                    except (TypeError, ValueError):
                        export_limit_w = None

        self._pre_control_export_limit_w = (
            export_limit_w if export_limit_enabled and export_limit_w is not None else None
        )
        self._pre_control_export_limit_captured = True

    async def _resolve_normal_discharge_limit_kw(self) -> float | None:
        """Resolve the Sungrow discharge cap to restore for self-consumption.

        Sungrow's writable max-discharge register is both the current cap and the
        value we temporarily lower for manual force discharge. Prefer the highest
        known normal limit so self-consumption does not inherit a lower optimiser
        or manual cap.
        """
        candidates: list[float] = []

        def add_kw(value: Any) -> None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return
            if parsed > 0:
                candidates.append(parsed)

        def add_w(value: Any) -> None:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return
            if parsed > 0:
                candidates.append(parsed / 1000.0)

        coord_data = getattr(self, "data", None) or {}
        add_kw(coord_data.get("battery_max_discharge_power"))
        add_w(coord_data.get("battery_max_discharge_power_w"))
        add_kw(coord_data.get("discharge_rate_limit_kw"))
        add_kw(coord_data.get("battery_max_charge_power"))
        add_w(coord_data.get("battery_max_charge_power_w"))
        add_kw(coord_data.get("charge_rate_limit_kw"))

        try:
            live_data = await self._controller.get_battery_data()
        except Exception as err:
            _LOGGER.debug("Could not read live Sungrow limits for restore target: %s", err)
        else:
            add_kw(live_data.get("discharge_rate_limit_kw"))
            add_kw(live_data.get("charge_rate_limit_kw"))

        return max(candidates) if candidates else None

    async def _capture_charge_limit_for_restore(self) -> None:
        """Save the current Sungrow charge limit before a temporary cap."""
        if getattr(self, "_pre_control_charge_limit_kw", None) is not None:
            return

        current_limit_kw = None
        coord_data = getattr(self, "data", None) or {}
        try:
            current_limit_kw = coord_data.get("battery_max_charge_power")
            charge_limit_w = coord_data.get("battery_max_charge_power_w")
            if current_limit_kw is None and charge_limit_w:
                current_limit_kw = float(charge_limit_w) / 1000.0
            if current_limit_kw is None:
                live_data = await self._controller.get_battery_data()
                current_limit_kw = live_data.get("charge_rate_limit_kw")
            if current_limit_kw is not None and float(current_limit_kw) > 0:
                self._pre_control_charge_limit_kw = float(current_limit_kw)
        except Exception as err:
            _LOGGER.debug(
                "Could not capture Sungrow charge limit before temporary cap: %s",
                err,
            )

    async def _restore_captured_charge_limit(self) -> bool:
        """Restore a Sungrow charge limit saved before temporary control."""
        restore_limit_kw = getattr(self, "_pre_control_charge_limit_kw", None)
        if restore_limit_kw is None or restore_limit_kw <= 0:
            return True

        limit_ok = await self._controller.set_charge_rate_limit(restore_limit_kw)
        if limit_ok:
            self._pre_control_charge_limit_kw = None
        return bool(limit_ok)

    async def _restore_captured_discharge_limit(self) -> bool:
        """Restore a Sungrow discharge limit saved before temporary control."""
        captured_limit_kw = getattr(self, "_pre_control_discharge_limit_kw", None)
        if captured_limit_kw is None:
            return True

        if getattr(self._controller, "rate_limit_writable", None) is False:
            self._pre_control_discharge_limit_kw = None
            return True

        restore_limit_kw = await self._resolve_normal_discharge_limit_kw()
        if restore_limit_kw is None:
            restore_limit_kw = captured_limit_kw
        else:
            restore_limit_kw = max(restore_limit_kw, captured_limit_kw)
        if restore_limit_kw is None or restore_limit_kw <= 0:
            return True

        limit_ok = await self._controller.set_discharge_rate_limit(restore_limit_kw)
        if limit_ok:
            self._pre_control_discharge_limit_kw = None
        return bool(limit_ok)

    async def _restore_captured_export_limit(self) -> bool:
        """Restore a Sungrow export limit saved before temporary control."""
        if not getattr(self, "_pre_control_export_limit_captured", False):
            return True

        restore_limit_w = getattr(self, "_pre_control_export_limit_w", None)
        if restore_limit_w is None:
            limit_ok = await self._controller.set_export_limit(None)
        else:
            limit_ok = await self._controller.set_export_limit(int(restore_limit_w))

        if limit_ok:
            self._pre_control_export_limit_w = None
            self._pre_control_export_limit_captured = False
        return bool(limit_ok)

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore self-consumption mode and discharge limit after IDLE."""
        async with self._modbus_lock, self._controller:
            normal_ok = await self._controller.restore_normal()
            charge_limit_ok = await self._restore_captured_charge_limit()
            limit_ok = await self._restore_captured_discharge_limit()
            return bool(normal_ok and charge_limit_ok and limit_ok)

    async def set_charge_rate_limit(self, kw: float) -> bool:
        """Set maximum charge rate in kW.

        Args:
            kw: Maximum charge rate in kW

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            return await self._controller.set_charge_rate_limit(kw)

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        """Set maximum discharge rate in kW.

        Args:
            kw: Maximum discharge rate in kW

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            return await self._controller.set_discharge_rate_limit(kw)

    async def set_export_limit(self, watts: int | None) -> bool:
        """Set export power limit in watts.

        Args:
            watts: Export limit in watts, or None to disable

        Returns:
            True if successful
        """
        async with self._modbus_lock, self._controller:
            return await self._controller.set_export_limit(watts)

    async def async_shutdown(self) -> None:
        """Disconnect from Sungrow system on shutdown."""
        await self._controller.disconnect()


class DualSungrowCoordinator(DataUpdateCoordinator):
    """Coordinator that aggregates two Sungrow SH inverters.

    Wraps two SungrowEnergyCoordinator instances (primary = grid-facing,
    secondary = on primary's backup port) and presents a single coordinator
    interface to the optimizer.  Power values are summed, SOC is
    capacity-weighted, and commands are split across both inverters.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        coord1: SungrowEnergyCoordinator,
        coord2: SungrowEnergyCoordinator,
        soc_cap: int = 100,
        cap1_kwh: float = 25.6,
        cap2_kwh: float = 25.6,
    ) -> None:
        self._coord1 = coord1  # Primary (grid-facing)
        self._coord2 = coord2  # Secondary (on backup port)
        self._soc_cap = soc_cap  # Max SOC for grid-forming inverter (100 = disabled)
        self._cap1 = cap1_kwh
        self._cap2 = cap2_kwh
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_sungrow_dual",
            update_interval=timedelta(seconds=30),
        )

    # ------------------------------------------------------------------
    # SOC-proportional power splitting
    # ------------------------------------------------------------------

    async def _split_power(self, total_kw: float, prefer_lower_soc: bool) -> tuple[float, float]:
        """Split power between inverters proportionally to SOC.

        prefer_lower_soc=True for charging (fill the emptier one faster).
        prefer_lower_soc=False for discharging (drain the fuller one faster).
        Returns (power_kw_for_coord1, power_kw_for_coord2).
        """
        soc1 = (self._coord1.data or {}).get("battery_level", 50) or 50
        soc2 = (self._coord2.data or {}).get("battery_level", 50) or 50

        total_cap = self._cap1 + self._cap2

        if abs(soc1 - soc2) < 2:
            return total_kw * self._cap1 / total_cap, total_kw * self._cap2 / total_cap

        if prefer_lower_soc:
            w1 = max(1, 100 - soc1) * self._cap1
            w2 = max(1, 100 - soc2) * self._cap2
        else:
            w1 = max(1, soc1) * self._cap1
            w2 = max(1, soc2) * self._cap2

        total_w = w1 + w2
        p1 = total_kw * w1 / total_w
        p2 = total_kw * w2 / total_w
        _LOGGER.debug(
            "Split %.2f kW: inv1=%.2f kW (soc=%.0f%%, cap=%.1f), inv2=%.2f kW (soc=%.0f%%, cap=%.1f), prefer_lower=%s",
            total_kw, p1, soc1, self._cap1, p2, soc2, self._cap2, prefer_lower_soc,
        )
        return p1, p2

    @staticmethod
    def _power_limit_kw(data: dict[str, Any], direction: str) -> float | None:
        """Return a per-inverter force-mode power limit from coordinator data."""
        raw_w = data.get(f"battery_max_{direction}_power_w")
        if raw_w and raw_w > 0:
            return float(raw_w) / 1000.0

        raw_kw = data.get(f"battery_max_{direction}_power")
        if raw_kw and raw_kw > 0:
            return float(raw_kw)

        return None

    def _combined_power_limit_w(self, direction: str) -> int | None:
        limits_kw = [
            self._power_limit_kw(self._coord1.data or {}, direction),
            self._power_limit_kw(self._coord2.data or {}, direction),
        ]
        if any(limit is None or limit <= 0 for limit in limits_kw):
            return None
        return int(round(sum(float(limit) for limit in limits_kw) * 1000.0))

    def _max_split_kw(self, direction: str) -> tuple[float, float] | None:
        limit1 = self._power_limit_kw(self._coord1.data or {}, direction)
        limit2 = self._power_limit_kw(self._coord2.data or {}, direction)
        if not limit1 or not limit2:
            return None
        return limit1, limit2

    # ------------------------------------------------------------------
    # Data aggregation
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Aggregate data from both sub-coordinators."""
        d1 = self._coord1.data or {}
        d2 = self._coord2.data or {}

        if not d1 and not d2:
            raise UpdateFailed("No data from either Sungrow inverter")

        # Sum power values (kW)
        solar = (d1.get("solar_power", 0) or 0) + (d2.get("solar_power", 0) or 0)
        battery = (d1.get("battery_power", 0) or 0) + (d2.get("battery_power", 0) or 0)
        load = (d1.get("load_power", 0) or 0) + (d2.get("load_power", 0) or 0)
        # Grid: use primary only (it's the grid-facing inverter)
        grid = d1.get("grid_power", 0) or 0

        # Capacity-weighted SOC
        soc1 = d1.get("battery_level", 0) or 0
        soc2 = d2.get("battery_level", 0) or 0
        combined_soc = (soc1 * self._cap1 + soc2 * self._cap2) / (self._cap1 + self._cap2)

        # SOC divergence warning
        if abs(soc1 - soc2) > 5:
            _LOGGER.info(
                "Sungrow dual SOC divergence: inv1=%.1f%%, inv2=%.1f%% (delta=%.1f%%)",
                soc1, soc2, abs(soc1 - soc2),
            )

        # Enforce grid-forming inverter SOC cap
        if self._soc_cap < 100:
            max_soc1 = d1.get("max_soc")
            if max_soc1 is None or abs(max_soc1 - self._soc_cap) > 1:
                _LOGGER.info(
                    "Enforcing SOC cap: setting inv1 max_soc to %d%% (current register: %s)",
                    self._soc_cap, max_soc1,
                )
                await self._coord1.set_max_soc(self._soc_cap)

        # Combine energy summaries
        es1 = d1.get("energy_summary", {}) or {}
        es2 = d2.get("energy_summary", {}) or {}
        combined_energy = {}
        for key in (
            "pv_today_kwh", "grid_import_today_kwh", "grid_export_today_kwh",
            "charge_today_kwh", "discharge_today_kwh", "load_today_kwh",
            "import_cost_today", "export_earnings_today",
            "mtd_import_cost", "mtd_export_earnings", "mtd_load_kwh",
        ):
            combined_energy[key] = round(
                (es1.get(key, 0) or 0) + (es2.get(key, 0) or 0), 4
            )
        load_today = combined_energy.get("load_today_kwh", 0) or 0
        combined_energy["avg_cost_per_kwh_today"] = (
            round((combined_energy["import_cost_today"] - combined_energy["export_earnings_today"]) / load_today, 4)
            if load_today > 0 else None
        )
        mtd_load = combined_energy.get("mtd_load_kwh", 0) or 0
        combined_energy["avg_cost_per_kwh_mtd"] = (
            round((combined_energy["mtd_import_cost"] - combined_energy["mtd_export_earnings"]) / mtd_load, 4)
            if mtd_load > 0 else None
        )
        charge_limit_w = self._combined_power_limit_w("charge")
        discharge_limit_w = self._combined_power_limit_w("discharge")

        return {
            "solar_power": max(0, solar),
            "grid_power": grid,
            "battery_power": battery,
            "load_power": load,
            "battery_level": combined_soc,
            "last_update": dt_util.utcnow(),
            # Use primary's Sungrow-specific fields
            "battery_soh": d1.get("battery_soh"),
            "battery_voltage": d1.get("battery_voltage"),
            "battery_current": d1.get("battery_current"),
            "battery_temp": d1.get("battery_temp"),
            "inverter_temperature": d1.get("inverter_temperature"),
            "ems_mode": d1.get("ems_mode"),
            "ems_mode_name": d1.get("ems_mode_name"),
            "charge_cmd": d1.get("charge_cmd"),
            "min_soc": d1.get("min_soc"),
            "max_soc": d1.get("max_soc"),
            "backup_reserve": d1.get("backup_reserve"),
            "charge_rate_limit_kw": d1.get("charge_rate_limit_kw"),
            "discharge_rate_limit_kw": d1.get("discharge_rate_limit_kw"),
            "export_limit_w": d1.get("export_limit_w"),
            "export_limit_enabled": d1.get("export_limit_enabled"),
            "battery_max_charge_power_w": charge_limit_w,
            "battery_max_discharge_power_w": discharge_limit_w,
            "battery_max_charge_power": (
                charge_limit_w / 1000.0
                if charge_limit_w
                else None
            ),
            "battery_max_discharge_power": (
                discharge_limit_w / 1000.0
                if discharge_limit_w
                else None
            ),
            "energy_summary": combined_energy,
            # Per-inverter SOC for monitoring
            "battery_level_1": soc1,
            "battery_level_2": soc2,
        }

    # ------------------------------------------------------------------
    # Command splitting — delegate to both sub-coordinators
    # ------------------------------------------------------------------

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force charge on both inverters with SOC-proportional power split."""
        if power_w > 0:
            p1, p2 = await self._split_power(power_w / 1000, prefer_lower_soc=True)
            r1 = await self._coord1.force_charge(duration_minutes, power_w=p1 * 1000)
            r2 = await self._coord2.force_charge(duration_minutes, power_w=p2 * 1000)
        else:
            r1 = await self._coord1.force_charge(duration_minutes)
            r2 = await self._coord2.force_charge(duration_minutes)
        return r1 and r2

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force discharge on both inverters with SOC-proportional power split."""
        if power_w > 0:
            max_split = self._max_split_kw("discharge")
            if max_split and (power_w / 1000.0) >= sum(max_split):
                p1, p2 = max_split
            else:
                p1, p2 = await self._split_power(power_w / 1000, prefer_lower_soc=False)
            r1 = await self._coord1.force_discharge(duration_minutes, power_w=p1 * 1000)
            r2 = await self._coord2.force_discharge(duration_minutes, power_w=p2 * 1000)
        else:
            r1 = await self._coord1.force_discharge(duration_minutes)
            r2 = await self._coord2.force_discharge(duration_minutes)
        return r1 and r2

    async def force_grid_export(
        self,
        duration_minutes: int = 30,
        export_limit_w: float = 0,
    ) -> bool:
        """Force discharge both inverters while limiting site export on primary."""
        max_split = self._max_split_kw("discharge")
        if max_split:
            _p1, p2 = max_split
            r1 = await self._coord1.force_grid_export(
                duration_minutes,
                export_limit_w=export_limit_w,
            )
            if not r1:
                return False
            try:
                r2 = await self._coord2.force_discharge(
                    duration_minutes,
                    power_w=p2 * 1000,
                )
            except Exception:
                await self.restore_normal()
                raise
        else:
            r1 = await self._coord1.force_grid_export(
                duration_minutes,
                export_limit_w=export_limit_w,
            )
            if not r1:
                return False
            try:
                r2 = await self._coord2.force_discharge(duration_minutes)
            except Exception:
                await self.restore_normal()
                raise

        if not r2:
            await self.restore_normal()
        return r1 and r2

    async def restore_normal(self) -> bool:
        """Restore self-consumption on both inverters."""
        r1 = await self._coord1.restore_normal()
        r2 = await self._coord2.restore_normal()
        return r1 and r2

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set backup reserve on both inverters."""
        r1 = await self._coord1.set_backup_reserve(percent)
        r2 = await self._coord2.set_backup_reserve(percent)
        return r1 and r2

    async def set_backup_mode(self) -> bool:
        """Set idle/backup mode on both inverters."""
        r1 = await self._coord1.set_backup_mode()
        r2 = await self._coord2.set_backup_mode()
        return r1 and r2

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore work mode from idle on both inverters."""
        r1 = await self._coord1.restore_work_mode_from_idle()
        r2 = await self._coord2.restore_work_mode_from_idle()
        return r1 and r2

    async def set_charge_rate_limit(self, kw: float) -> bool:
        """Split charge rate proportionally between both inverters."""
        p1, p2 = await self._split_power(kw, prefer_lower_soc=True)
        r1 = await self._coord1.set_charge_rate_limit(p1)
        r2 = await self._coord2.set_charge_rate_limit(p2)
        return r1 and r2

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        """Split discharge rate proportionally between both inverters."""
        p1, p2 = await self._split_power(kw, prefer_lower_soc=False)
        r1 = await self._coord1.set_discharge_rate_limit(p1)
        r2 = await self._coord2.set_discharge_rate_limit(p2)
        return r1 and r2

    async def set_max_soc(self, percent: int) -> bool:
        """Set max SOC on primary (grid-forming) inverter only."""
        return await self._coord1.set_max_soc(percent)

    async def set_export_limit(self, watts: int | None) -> bool:
        """Set export limit on primary only (it's grid-facing)."""
        return await self._coord1.set_export_limit(watts)

    async def async_shutdown(self) -> None:
        """Shutdown both sub-coordinators."""
        await self._coord1.async_shutdown()
        await self._coord2.async_shutdown()


class FoxESSEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch FoxESS battery system data via Modbus.

    Polls the FoxESS inverter via Modbus TCP or RS485 to get real-time
    power data (solar, battery, grid, load), battery SOC, and control settings.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 502,
        slave_id: int = 247,
        connection_type: str = "tcp",
        serial_port: str | None = None,
        baudrate: int = 9600,
        model_family: str | None = None,
        entry_id: str = "",
    ) -> None:
        """Initialize the coordinator."""
        from .inverters.foxess import FoxESSController

        self.host = host
        self.port = port
        self.slave_id = slave_id
        self._entry_id = entry_id
        self._controller = FoxESSController(
            host=host,
            port=port,
            slave_id=slave_id,
            connection_type=connection_type,
            serial_port=serial_port,
            baudrate=baudrate,
            model_family=model_family,
        )

        self._energy_acc = EnergyAccumulator(hass, "foxess")

        # Serialise all Modbus access so that data polls (every 30s) can't
        # clobber an in-progress force charge/discharge. Without this, the
        # data poll's connect() closes the TCP connection that force charge
        # opened, causing the reg=46003 write to fail silently (the
        # _connected=False guard fires before the DEBUG log, so no WRITE or
        # verify log appears — just "write failed on attempt N/3").
        self._modbus_lock = asyncio.Lock()

        super().__init__(
            hass,
            _LOGGER,
            name="FoxESS Energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from FoxESS system via Modbus."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        try:
            async with self._modbus_lock, self._controller:
                status = await self._controller.get_status()
                energy_summary = await self._controller.get_energy_summary()

            if not status.attributes:
                raise UpdateFailed("No data from FoxESS controller")

            attrs = status.attributes

            # Map to standard format (convention: positive = discharging, negative = charging)
            battery_kw = attrs.get("battery_power_kw", 0) or 0
            grid_kw = attrs.get("grid_power_kw", 0) or 0
            load_kw = attrs.get("load_power_kw", 0) or 0
            solar_kw = attrs.get("pv_power_kw", 0) or 0
            ct2_kw = attrs.get("ct2_power_kw", 0) or 0

            # Total solar = DC PV strings + AC-coupled CT2 meter
            total_solar_kw = solar_kw + max(0, ct2_kw)

            # Accumulate daily energy from power readings (with cost tracking)
            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(total_solar_kw, grid_kw, battery_kw, load_kw, buy, sell)

            # Merge Modbus energy registers (charge/discharge) with accumulated values
            acc = self._energy_acc.as_dict()
            if energy_summary:
                # Prefer Modbus registers for charge/discharge (more accurate)
                acc["charge_today_kwh"] = energy_summary.get("charge_today_kwh", acc["charge_today_kwh"])
                acc["discharge_today_kwh"] = energy_summary.get("discharge_today_kwh", acc["discharge_today_kwh"])

            energy_data = {
                "solar_power": max(0, total_solar_kw),
                "ct2_power": ct2_kw,
                "pv1_power": attrs.get("pv1_power_kw", 0) or 0,
                "pv2_power": attrs.get("pv2_power_kw", 0) or 0,
                "pv3_power": attrs.get("pv3_power_kw", 0) or 0,
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "battery_level": attrs.get("battery_soc", 0),
                "last_update": dt_util.utcnow(),
                # FoxESS-specific data
                "work_mode": attrs.get("work_mode"),
                "work_mode_name": attrs.get("work_mode_name"),
                "min_soc": attrs.get("min_soc"),
                "max_charge_current_a": attrs.get("max_charge_current_a"),
                "max_discharge_current_a": attrs.get("max_discharge_current_a"),
                "battery_voltage_v": attrs.get("battery_voltage_v"),
                "battery_temperature": attrs.get("battery_temperature"),
                "model_family": attrs.get("model_family"),
                "energy_summary": acc,
                "battery_soh": attrs.get("soh"),
                "nominal_power_w": attrs.get("nominal_power_w"),
                "nominal_energy_kwh": attrs.get("nominal_energy_kwh"),
                "total_charged_energy_kwh": attrs.get("total_charged_energy_kwh"),
            }

            # Max charge/discharge power is taken directly from nominal_power_w
            # (register 39053 on H3-Smart). Empirically this matches the inverter's
            # rated capacity and is more reliable than current×voltage arithmetic.
            _nominal_w = attrs.get("nominal_power_w")
            if _nominal_w and _nominal_w > 0:
                energy_data["battery_max_charge_power_w"] = int(_nominal_w)
                energy_data["battery_max_charge_power"] = round(_nominal_w / 1000.0, 2)
                energy_data["battery_max_discharge_power_w"] = int(_nominal_w)
                energy_data["battery_max_discharge_power"] = round(_nominal_w / 1000.0, 2)

            _LOGGER.debug(
                "FoxESS data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW, mode=%s",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
                energy_data.get("work_mode_name", "?"),
            )

            return energy_data

        except Exception as err:
            raise UpdateFailed(f"Error fetching FoxESS energy data: {err}") from err

    # Per-model fallback voltage for current→power conversion when the live
    # pack voltage read is missing. HV families (H3-Pro, H3-Smart) run around
    # 500 V nominal; LV families (H1, H3, KH) around 51.2 V. The previous
    # single 300 V fallback silently capped HV systems at 50 A × 300 V = 15 kW.
    _FALLBACK_PACK_VOLTAGE = {
        "H3-Pro": 500,
        "H3-Smart": 500,
        "H1": 51.2,
        "H3": 51.2,
        "KH": 51.2,
    }

    def _resolve_pack_voltage_from_attrs(self, attrs: dict | None) -> float:
        """Pick the best pack voltage from an attrs dict, with model-aware fallback."""
        v = (attrs or {}).get("battery_voltage_v")
        if isinstance(v, (int, float)) and v > 100:
            return float(v)
        family = getattr(getattr(self, "_controller", None), "_model_family", None)
        family_str = family.value if family and hasattr(family, "value") else None
        return float(self._FALLBACK_PACK_VOLTAGE.get(family_str, 300))

    def _resolve_pack_voltage(self, for_logging: str = "") -> float:
        """Pick the best pack voltage we have, falling back by model family.

        Uses self.data (most recent coordinator refresh) as the source and
        logs when we fall back so a misbehaving voltage register is visible.
        """
        v = (self.data or {}).get("battery_voltage_v")
        if isinstance(v, (int, float)) and v > 100:
            return float(v)

        family = getattr(getattr(self, "_controller", None), "_model_family", None)
        family_str = family.value if family and hasattr(family, "value") else None
        fallback = self._FALLBACK_PACK_VOLTAGE.get(family_str, 300)
        _LOGGER.warning(
            "FoxESS%s: live battery voltage unavailable (got %r), "
            "falling back to %sV based on model family %s",
            f" {for_logging}" if for_logging else "",
            v,
            fallback,
            family_str or "UNKNOWN",
        )
        return float(fallback)

    async def force_charge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: int = 600,
    ) -> bool:
        """Set FoxESS to force charge mode.

        Args:
            duration_minutes: How long to charge
            power_w: Charge power in watts. If 0, reads max_charge_current from
                     the inverter and uses that (respects user's FoxESS app setting).
            min_timeout_seconds: Minimum hardware remote-control timeout.
        """
        async with self._modbus_lock, self._controller:
            if power_w <= 0 and self.data:
                # Use inverter's configured max charge current (set via FoxESS app)
                max_charge_a = self.data.get("max_charge_current_a")
                if max_charge_a and max_charge_a > 0:
                    voltage = self._resolve_pack_voltage("force_charge")
                    power_w = max_charge_a * voltage
                    _LOGGER.info(
                        "FoxESS force_charge using inverter max: %.0fA × %.0fV → %.0fW",
                        max_charge_a, voltage, power_w,
                    )
            if power_w <= 0:
                power_w = 5000  # Fallback default
            return await self._controller.force_charge(
                duration_minutes,
                power_w=power_w,
                min_timeout_seconds=min_timeout_seconds,
            )

    async def force_discharge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: int = 600,
    ) -> bool:
        """Set FoxESS to force discharge mode.

        Args:
            duration_minutes: How long to discharge
            power_w: Discharge power in watts. If 0, reads max_discharge_current from
                     the inverter and uses that (respects user's FoxESS app setting).
        """
        async with self._modbus_lock, self._controller:
            if power_w <= 0 and self.data:
                # Use inverter's configured max discharge current (set via FoxESS app)
                max_discharge_a = self.data.get("max_discharge_current_a")
                if max_discharge_a and max_discharge_a > 0:
                    voltage = self._resolve_pack_voltage("force_discharge")
                    power_w = max_discharge_a * voltage
                    _LOGGER.info(
                        "FoxESS force_discharge using inverter max: %.0fA × %.0fV → %.0fW",
                        max_discharge_a, voltage, power_w,
                    )
            if power_w <= 0:
                power_w = 5000  # Fallback default
            return await self._controller.force_discharge(
                duration_minutes,
                power_w=power_w,
                min_timeout_seconds=min_timeout_seconds,
            )

    async def restore_normal(self) -> bool:
        """Restore FoxESS to normal (Self Use) operation."""
        async with self._modbus_lock, self._controller:
            return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum SOC (backup reserve)."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_backup_reserve(percent)

    async def set_backup_mode(self) -> bool:
        """Set FoxESS to Backup mode (IDLE — prevents self-consumption discharge)."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_backup_mode()

    async def set_no_discharge_mode(self) -> bool:
        """Block FoxESS self-consumption discharge while still allowing charge."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_backup_mode()

    async def restore_no_discharge_mode(self) -> bool:
        """Restore FoxESS from scheduled EV no-discharge preserve mode."""
        async with self._modbus_lock, self._controller:
            return await self._controller.restore_work_mode_from_idle()

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore work mode to Self Use after IDLE Backup mode."""
        async with self._modbus_lock, self._controller:
            return await self._controller.restore_work_mode_from_idle()

    async def set_work_mode(self, mode: int) -> bool:
        """Set FoxESS work mode."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_work_mode(mode)

    async def set_charge_rate_limit(self, amps: float) -> bool:
        """Set maximum charge current in amps."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_charge_rate_limit(amps)

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        """Set maximum discharge current in amps."""
        async with self._modbus_lock, self._controller:
            return await self._controller.set_discharge_rate_limit(amps)

    async def curtail(self, home_load_w: int | None = None) -> bool:
        """Apply FoxESS solar export curtailment via the shared Modbus session."""
        async with self._modbus_lock, self._controller:
            return await self._controller.curtail(home_load_w)

    async def restore_curtailment(self) -> bool:
        """Restore FoxESS solar export after curtailment via the shared Modbus session."""
        async with self._modbus_lock, self._controller:
            return await self._controller.restore()

    async def async_shutdown(self) -> None:
        """Disconnect from FoxESS system on shutdown."""
        await self._controller.disconnect()


class FoxESSEntityEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for FoxESS via nathanmarlor/foxess_modbus entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        foxess_entry_id: str | None = None,
        entity_prefix: str = "",
        entry_id: str = "",
    ) -> None:
        from .inverters.foxess_entity import FoxESSEntityController

        self._entry_id = entry_id
        self._controller = FoxESSEntityController(
            hass,
            foxess_entry_id=foxess_entry_id,
            entity_prefix=entity_prefix,
        )
        self._energy_acc = EnergyAccumulator(hass, "foxess_entity")
        self._validated = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_foxess_entity_energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return FoxESS data assembled from foxess_modbus entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        try:
            if not self._validated:
                await self._controller.connect()
                self._validated = True
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning(
                    "FoxESS entity bridge read failed, returning stale data: %s",
                    exc,
                )
                return self.data
            raise UpdateFailed(f"FoxESS entity bridge read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level", 0.0) or 0.0

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)
        energy_summary = self._energy_acc.as_dict()
        for status_key, summary_key in (
            ("daily_solar_energy_kwh", "pv_today_kwh"),
            ("daily_grid_import_kwh", "grid_import_today_kwh"),
            ("daily_grid_export_kwh", "grid_export_today_kwh"),
            ("daily_battery_charge_kwh", "charge_today_kwh"),
            ("daily_battery_discharge_kwh", "discharge_today_kwh"),
        ):
            value = status.get(status_key)
            if isinstance(value, (int, float)) and value >= 0:
                energy_summary[summary_key] = round(float(value), 3)

        data = {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "last_update": dt_util.utcnow(),
            "battery_temperature": status.get("battery_temperature"),
            "battery_soh": status.get("battery_soh"),
            "backup_reserve": status.get("backup_reserve"),
            "min_soc": status.get("min_soc"),
            "mode": status.get("mode"),
            "work_mode": status.get("work_mode"),
            "work_mode_name": status.get("work_mode_name"),
            "max_charge_current_a": status.get("max_charge_current_a"),
            "max_discharge_current_a": status.get("max_discharge_current_a"),
            "energy_summary": energy_summary,
        }
        for key in (
            "battery_max_charge_power_w",
            "battery_max_charge_power",
            "battery_max_discharge_power_w",
            "battery_max_discharge_power",
        ):
            if status.get(key) is not None:
                data[key] = status[key]
        for idx in range(1, 7):
            for suffix in ("power", "voltage", "current"):
                key = f"pv{idx}_{suffix}"
                if status.get(key) is not None:
                    data[key] = status[key]

        _LOGGER.debug(
            "FoxESS entity data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW, mode=%s",
            data["solar_power"],
            data["grid_power"],
            data["battery_power"],
            data["battery_level"],
            data["load_power"],
            data.get("work_mode_name", "?"),
        )

        return data

    async def force_charge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: float | None = None,
    ) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: float | None = None,
    ) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._controller.set_backup_reserve(percent)

    async def get_backup_reserve(self) -> int | None:
        return await self._controller.get_backup_reserve()

    async def set_backup_mode(self) -> bool:
        return await self._controller.set_backup_mode()

    async def restore_work_mode_from_idle(self) -> bool:
        return await self._controller.restore_work_mode_from_idle()

    async def set_work_mode(self, mode: int | str) -> bool:
        return await self._controller.set_work_mode(mode)

    async def set_operation_mode(self, mode: str) -> bool:
        return await self._controller.set_operation_mode(mode)

    async def set_charge_rate_limit(self, amps: float) -> bool:
        return await self._controller.set_charge_rate_limit(amps)

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        return await self._controller.set_discharge_rate_limit(amps)

    async def curtail(self, home_load_w: int | None = None) -> bool:
        return await self._controller.curtail(home_load_w)

    async def restore_curtailment(self) -> bool:
        return await self._controller.restore()

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class FoxESSCloudEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch and control FoxESS systems through FoxESS Cloud."""

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        device_sn: str,
        entry_id: str = "",
    ) -> None:
        """Initialize the cloud coordinator."""
        from .foxess_api import FoxESSCloudClient

        self._entry_id = entry_id
        self.device_sn = device_sn
        self._client = FoxESSCloudClient(
            api_key=api_key,
            device_sn=device_sn,
            session=async_get_clientsession(hass),
        )
        # Keep compatibility with older curtailment code paths that reached for
        # foxess_coordinator._controller.curtail()/restore().
        self._controller = self
        self._energy_acc = EnergyAccumulator(hass, "foxess_cloud")
        self._store = Store(hass, 1, f"{DOMAIN}.foxess_cloud.{entry_id}") if entry_id else None
        self._stored_scheduler_groups: list[dict] | None = None
        self._last_backup_reserve = 10

        super().__init__(
            hass,
            _LOGGER,
            name="FoxESS Cloud Energy",
            update_interval=timedelta(seconds=60),
        )

    @staticmethod
    def _to_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _real_data_map(payload: Any) -> dict[str, Any]:
        """Flatten FoxESS realtime response variants into variable -> value."""
        if isinstance(payload, dict) and isinstance(payload.get("datas"), list):
            rows = payload["datas"]
        elif isinstance(payload, list) and payload:
            first = payload[0]
            rows = first.get("datas", []) if isinstance(first, dict) else []
        elif isinstance(payload, dict):
            result = payload.get("data") or payload.get("result")
            if isinstance(result, list) and result:
                rows = result[0].get("datas", []) if isinstance(result[0], dict) else []
            else:
                rows = []
        else:
            rows = []

        data = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            key = item.get("variable") or item.get("key") or item.get("name")
            if key:
                data[str(key)] = item.get("value")
        return data

    @staticmethod
    def _soc_from_values(values: dict[str, Any]) -> float | None:
        """Return battery SoC (%) from a flattened realtime map, or None if absent.

        FoxESS Cloud realtime can omit the SoC variable for a device (cloud lag,
        model variant, or a transient gap) or return it as null. Distinguish a
        missing reading from a genuine 0% so callers can keep the previous SOC
        instead of telling the optimizer the battery is empty.
        """
        raw = values.get("SoC")
        if raw is None:
            raw = values.get("soc")
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    async def _load_stored_scheduler(self) -> None:
        if self._stored_scheduler_groups is not None or not self._store:
            return
        try:
            stored = await self._store.async_load()
        except Exception as err:
            _LOGGER.debug("FoxESS Cloud: failed to load stored scheduler state: %s", err)
            stored = None
        self._stored_scheduler_groups = (stored or {}).get("scheduler_groups")

    async def _save_current_scheduler(self) -> None:
        """Persist current non-hidden scheduler groups before a temporary action."""
        if self._stored_scheduler_groups:
            return
        try:
            from .foxess_api import filter_public_scheduler_groups

            result = await self._client.get_scheduler()
            groups = []
            if isinstance(result, dict):
                groups = result.get("groups") or result.get("schedulerList") or []
            self._stored_scheduler_groups = filter_public_scheduler_groups(groups)
            if self._store:
                await self._store.async_save({"scheduler_groups": self._stored_scheduler_groups})
        except Exception as err:
            _LOGGER.warning("FoxESS Cloud: failed to snapshot scheduler before control action: %s", err)
            self._stored_scheduler_groups = []

    async def _restore_stored_scheduler(self) -> bool:
        await self._load_stored_scheduler()
        if self._stored_scheduler_groups is not None:
            await self._client.set_scheduler_v3(self._stored_scheduler_groups)
        if self._store:
            await self._store.async_save({"scheduler_groups": []})
        self._stored_scheduler_groups = []
        return True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch FoxESS Cloud realtime data."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        try:
            raw = await self._client.get_real_data()
            values = self._real_data_map(raw)

            def _first_present(*keys: str) -> Any:
                for key in keys:
                    val = values.get(key)
                    if val is not None:
                        return val
                return None

            solar_kw = self._to_float(values.get("pvPower") or values.get("generationPower")) / 1000.0
            load_kw = self._to_float(values.get("loadsPower")) / 1000.0

            # Battery power: FoxESS exposes different variables per model. KH/K-series
            # report invBatPower (or split batChargePower/batDischargePower) rather than
            # batPower, so reading batPower alone yields 0 on those units. Prefer the
            # signed inverter-battery reading, then fall back to charge/discharge
            # magnitudes. Positive = discharging, matching the PowerSync convention.
            inv_bat = _first_present("invBatPower", "batPower")
            if inv_bat is not None:
                battery_kw = self._to_float(inv_bat) / 1000.0
            else:
                charge_kw = self._to_float(_first_present("batChargePower", "chargePower")) / 1000.0
                discharge_kw = self._to_float(_first_present("batDischargePower", "dischargePower")) / 1000.0
                battery_kw = discharge_kw - charge_kw

            # Grid power: prefer the meter reading; otherwise net import minus export.
            meter = _first_present("meterPower")
            if meter is not None:
                grid_kw = self._to_float(meter) / 1000.0
            else:
                import_kw = self._to_float(values.get("gridConsumptionPower")) / 1000.0
                export_kw = self._to_float(values.get("feedinPower")) / 1000.0
                grid_kw = import_kw - export_kw

            # If FoxESS Cloud realtime returned no usable SoC, keep the previous
            # readings rather than reporting SOC=0% — a 0% reading makes the
            # optimizer think the battery is empty and schedule IDLE. This mirrors
            # the Sungrow/Sigenergy Modbus coordinators' missing-battery-data guard.
            soc = self._soc_from_values(values)
            if soc is None:
                if self.data:
                    _LOGGER.warning(
                        "FoxESS Cloud realtime returned no battery SoC — keeping previous readings"
                    )
                    return self.data
                raise UpdateFailed(
                    "FoxESS Cloud realtime returned no battery SoC — no data available"
                )

            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(solar_kw, grid_kw, battery_kw, load_kw, buy, sell)
            acc = self._energy_acc.as_dict()

            charge_total = self._to_float(values.get("chargeEnergyToTal"), acc["charge_today_kwh"])
            discharge_total = self._to_float(values.get("dischargeEnergyToTal"), acc["discharge_today_kwh"])
            acc["charge_today_kwh"] = charge_total
            acc["discharge_today_kwh"] = discharge_total

            data = {
                "solar_power": max(0, solar_kw),
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "battery_level": soc,
                "last_update": dt_util.utcnow(),
                "work_mode": values.get("workMode"),
                "work_mode_name": values.get("workMode"),
                "energy_summary": acc,
                "cloud_backend": True,
            }
            _LOGGER.debug(
                "FoxESS Cloud data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW, load=%.2f kW, soc=%.0f%%",
                data["solar_power"], data["grid_power"], data["battery_power"],
                data["load_power"], data["battery_level"],
            )
            return data
        except Exception as err:
            raise UpdateFailed(f"Error fetching FoxESS Cloud energy data: {err}") from err

    async def force_charge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: int = 600,
    ) -> bool:
        """Set FoxESS to force charge mode through Scheduler V3."""
        await self._save_current_scheduler()
        await self._client.force_charge(
            duration_minutes,
            power_w=power_w,
            target_soc=100,
            min_soc=self._last_backup_reserve,
        )
        return True

    async def force_discharge(
        self,
        duration_minutes: int = 30,
        power_w: float = 0,
        min_timeout_seconds: int = 600,
    ) -> bool:
        """Set FoxESS to force discharge mode through Scheduler V3."""
        await self._save_current_scheduler()
        await self._client.force_discharge(
            duration_minutes,
            power_w=power_w,
            target_soc=self._last_backup_reserve,
            min_soc=self._last_backup_reserve,
        )
        return True

    async def restore_normal(self) -> bool:
        """Restore scheduler state and set SelfUse mode."""
        await self._restore_stored_scheduler()
        await self._client.set_work_mode("SelfUse")
        return True

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum SOC through FoxESS Cloud."""
        value = int(max(0, min(100, percent)))
        self._last_backup_reserve = value
        await self._client.set_battery_soc(value, value)
        return True

    async def set_backup_mode(self) -> bool:
        """Set FoxESS Backup mode through cloud settings."""
        await self._client.set_work_mode("Backup")
        return True

    async def restore_work_mode_from_idle(self) -> bool:
        """Restore work mode to SelfUse after idle hold."""
        return await self.restore_normal()

    async def set_work_mode(self, mode: int | str) -> bool:
        """Set FoxESS work mode through cloud settings."""
        mode_map = {0: "SelfUse", 1: "FeedIn", 2: "Backup"}
        cloud_mode = mode_map.get(mode, mode)
        await self._client.set_work_mode(cloud_mode)
        return True

    async def set_charge_rate_limit(self, amps: float) -> bool:
        """Set maximum charge current in amps."""
        await self._client.set_device_setting("MaxSetChargeCurrent", float(amps))
        return True

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        """Set maximum discharge current in amps."""
        await self._client.set_device_setting("MaxSetDischargeCurrent", float(amps))
        return True

    async def curtail(self, home_load_w: int | None = None) -> bool:
        """Curtail export through FoxESS Cloud export limit settings.

        On FoxESS the ``ExportLimit`` key is the export ceiling in watts (not a
        0/1 enable). ``ExportLimitPower``/``ActivePowerLimit`` are best-effort —
        not every model exposes them — so writes that report the key unsupported
        are tolerated rather than aborting the curtailment.
        """
        await self._save_current_scheduler()
        limit = max(0, float(home_load_w or 0))
        await self._client.set_device_setting("ExportLimit", limit)
        await self._client.set_device_setting_optional("ExportLimitPower", limit)
        await self._client.set_device_setting_optional("ActivePowerLimit", limit)
        await self._client.set_scheduler_v3(
            [
                {
                    "startHour": 0,
                    "startMinute": 0,
                    "endHour": 23,
                    "endMinute": 59,
                    "workMode": "SelfUse",
                    "exportLimit": limit,
                    "pvLimit": limit,
                    "minSocOnGrid": self._last_backup_reserve,
                }
            ]
        )
        return True

    async def restore(self) -> bool:
        """Compatibility alias for curtailment restore."""
        return await self.restore_curtailment()

    async def restore_curtailment(self) -> bool:
        """Restore export limit after cloud curtailment."""
        await self._restore_stored_scheduler()
        await self._client.set_device_setting("ExportLimit", 30000)
        await self._client.set_device_setting_optional("ExportLimitPower", 30000)
        await self._client.set_device_setting_optional("ActivePowerLimit", 30000)
        return True

    async def async_shutdown(self) -> None:
        """Shutdown cloud coordinator."""
        await self._energy_acc.async_flush()
        await self._client.close()


class GoodWeEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch GoodWe battery system data via goodwe library.

    Polls the GoodWe inverter to get real-time power data (solar, battery,
    grid, load), battery SOC, and provides battery control.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int = 8899,
        comm_addr: int = 0,
        entry_id: str = "",
        ems_entity_prefix: str | None = None,
        entity_telemetry_prefix: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        from .inverters.goodwe_battery import GoodWeBatteryController

        self.host = host
        self.port = port
        self._entry_id = entry_id
        # When ems_entity_prefix is set (e.g. "goodwe"), control commands are
        # relayed through the community GoodWe HA integration's EMS entities
        # (select.<prefix>_ems_mode, number.<prefix>_ems_power_limit) instead of
        # opening a direct UDP connection.  This is necessary when the inverter is
        # only reachable via a Modbus TCP gateway — the EMS mode registers accept
        # Modbus TCP writes whereas the standard operation-mode registers do not.
        self._ems_prefix = ems_entity_prefix
        self._controller = GoodWeBatteryController(
            host=host, port=port, comm_addr=comm_addr
        )
        self._telemetry_controller = self._controller
        self._entity_telemetry_prefix = (entity_telemetry_prefix or "").strip()
        self._using_entity_telemetry = bool(self._entity_telemetry_prefix)
        if self._using_entity_telemetry:
            from .inverters.goodwe_entity import GoodWeEntityTelemetryController

            self._telemetry_controller = GoodWeEntityTelemetryController(
                hass,
                entity_prefix=self._entity_telemetry_prefix,
            )
        self._connected = False
        self._telemetry_validated = False
        self._energy_acc = EnergyAccumulator(hass, "goodwe")
        self._discharge_floor_pct: int = 10  # updated by set_backup_reserve

        super().__init__(
            hass,
            _LOGGER,
            name="GoodWe Energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from GoodWe inverter."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()
        try:
            if self._using_entity_telemetry:
                if not self._telemetry_validated:
                    await self._telemetry_controller.connect()
                    self._telemetry_validated = True
                data = self._telemetry_controller.get_runtime_data()
            else:
                if not self._connected:
                    await self._controller.connect()
                    self._connected = True

                data = await self._controller.get_runtime_data()

            solar_kw = data["solar_power"]
            grid_kw = data["grid_power"]
            battery_kw = data["battery_power"]
            load_kw = data["load_power"]

            # Accumulate daily energy from power readings (with cost tracking)
            buy, sell = _get_current_prices(self.hass, self._entry_id)
            self._energy_acc.update(max(0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

            energy_data = {
                "solar_power": solar_kw,
                "grid_power": grid_kw,
                "battery_power": battery_kw,
                "load_power": load_kw,
                "battery_level": data["battery_level"],
                "last_update": dt_util.utcnow(),
                # GoodWe-specific
                "battery_temperature": data.get("battery_temperature"),
                "battery_soh": data.get("battery_soh"),
                "model_name": data.get("model_name"),
                "serial_number": data.get("serial_number"),
                "rated_power_w": data.get("rated_power_w"),
                # Inverter nameplate rating as the BMS ceiling — GoodWe ET/EH
                # hybrid inverters match their battery's charge/discharge rate
                # to rated_power_w in practice, so reuse it as the force-mode
                # picker's Max value. Symmetric for charge + discharge.
                "battery_max_charge_power_w": data.get("rated_power_w"),
                "battery_max_discharge_power_w": data.get("rated_power_w"),
                "battery_max_charge_power": (
                    round(data["rated_power_w"] / 1000.0, 2)
                    if data.get("rated_power_w") else None
                ),
                "battery_max_discharge_power": (
                    round(data["rated_power_w"] / 1000.0, 2)
                    if data.get("rated_power_w") else None
                ),
                "energy_summary": self._energy_acc.as_dict(),
            }
            if data.get("work_mode") is not None:
                energy_data["work_mode"] = data.get("work_mode")
                energy_data["work_mode_name"] = data.get("work_mode_name")
            if data.get("entity_telemetry"):
                energy_data["entity_telemetry"] = True
            for status_key, summary_key in (
                ("daily_solar_energy_kwh", "pv_today_kwh"),
                ("daily_grid_import_kwh", "grid_import_today_kwh"),
                ("daily_grid_export_kwh", "grid_export_today_kwh"),
                ("daily_battery_charge_kwh", "charge_today_kwh"),
                ("daily_battery_discharge_kwh", "discharge_today_kwh"),
            ):
                value = data.get(status_key)
                if isinstance(value, (int, float)) and value >= 0:
                    energy_data["energy_summary"][summary_key] = round(float(value), 3)

            _LOGGER.debug(
                "GoodWe data%s: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW",
                " (entity telemetry)" if self._using_entity_telemetry else "",
                energy_data["solar_power"],
                energy_data["grid_power"],
                energy_data["battery_power"],
                energy_data["battery_level"],
                energy_data["load_power"],
            )

            return energy_data

        except Exception as err:
            if self._using_entity_telemetry and self.data:
                _LOGGER.warning(
                    "GoodWe entity telemetry read failed, returning stale data: %s",
                    err,
                )
                return self.data
            if self._using_entity_telemetry:
                self._telemetry_validated = False
            else:
                self._connected = False
            raise UpdateFailed(f"Error fetching GoodWe data: {err}") from err

    def _goodwe_ems_mode_attempts(
        self,
        mode_entity: str,
        preferred_option: str,
        fallback_option: str | None = None,
    ) -> list[str]:
        """Return supported GoodWe EMS mode attempts in preference order."""
        attempts = [preferred_option]
        if fallback_option and fallback_option != preferred_option:
            attempts.append(fallback_option)

        state = self.hass.states.get(mode_entity)
        raw_options = state.attributes.get("options") if state else None
        if not isinstance(raw_options, (list, tuple, set)):
            return attempts

        options = {str(option) for option in raw_options}
        if preferred_option in options:
            return [preferred_option]
        if fallback_option and fallback_option in options:
            _LOGGER.warning(
                "GoodWe EMS mode %s is not exposed by %s; falling back to %s",
                preferred_option,
                mode_entity,
                fallback_option,
            )
            return [fallback_option]

        _LOGGER.warning(
            "GoodWe EMS modes %s are not exposed by %s (available: %s); trying %s",
            attempts,
            mode_entity,
            sorted(options),
            preferred_option,
        )
        return [preferred_option]

    async def _ems_set_mode(
        self,
        ems_option: str,
        power_w: float,
        fallback_option: str | None = None,
        reset_power_limit: bool = False,
        restore_operation_mode: bool = False,
    ) -> bool:
        """Control via the community GoodWe HA integration's EMS entities.

        Uses select.<prefix>_ems_mode and number.<prefix>_ems_power_limit.
        These registers accept Modbus TCP writes, unlike the standard
        operation-mode / work-mode registers which require UDP.
        """
        p = self._ems_prefix
        mode_entity = f"select.{p}_ems_mode"
        power_entity = f"number.{p}_ems_power_limit"

        # GoodWe EMS power limit register is 16-bit unsigned, max 32768 W
        GOODWE_EMS_MAX_W = 32768
        try:
            power_limit_log: int | str = "unchanged"
            if power_w > 0:
                capped_w = min(int(power_w), GOODWE_EMS_MAX_W)
                await self.hass.services.async_call(
                    "number", "set_value",
                    {"entity_id": power_entity, "value": capped_w},
                    blocking=True,
                )
                power_limit_log = capped_w
            elif reset_power_limit:
                state = self.hass.states.get(power_entity)
                rated_power_w = (self.data or {}).get("rated_power_w")
                try:
                    restore_limit = int(float(rated_power_w))
                    if restore_limit <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    raw_max = state.attributes.get("max") if state else None
                    try:
                        restore_limit = int(float(raw_max))
                    except (TypeError, ValueError):
                        restore_limit = GOODWE_EMS_MAX_W
                restore_limit = max(1, min(restore_limit, GOODWE_EMS_MAX_W))
                try:
                    await self.hass.services.async_call(
                        "number", "set_value",
                        {"entity_id": power_entity, "value": restore_limit},
                        blocking=True,
                    )
                    power_limit_log = restore_limit
                except Exception as reset_exc:
                    _LOGGER.warning(
                        "GoodWe EMS control could not reset %s power limit to %dW: %s",
                        power_entity,
                        restore_limit,
                        reset_exc,
                    )

            attempts = self._goodwe_ems_mode_attempts(
                mode_entity,
                ems_option,
                fallback_option,
            )
            last_exc: Exception | None = None
            for option in attempts:
                try:
                    await self.hass.services.async_call(
                        "select",
                        "select_option",
                        {"entity_id": mode_entity, "option": option},
                        blocking=True,
                    )
                    _LOGGER.info(
                        "GoodWe EMS control: set %s=%s power_limit=%sW",
                        mode_entity,
                        option,
                        power_limit_log,
                    )
                    if restore_operation_mode:
                        await self._ems_restore_operation_mode()
                    return True
                except Exception as select_exc:
                    last_exc = select_exc
                    if option != attempts[-1]:
                        _LOGGER.warning(
                            "GoodWe EMS control failed for %s=%s; trying %s: %s",
                            mode_entity,
                            option,
                            attempts[-1],
                            select_exc,
                        )

            if last_exc:
                raise last_exc
            return False
        except Exception as exc:
            _LOGGER.error("GoodWe EMS control failed (%s=%s): %s", mode_entity, ems_option, exc)
            return False

    async def _ems_restore_operation_mode(self) -> None:
        """Best-effort restore of the companion GoodWe operation-mode select."""
        p = self._ems_prefix
        operation_entity = f"select.{p}_inverter_operation_mode"
        state = self.hass.states.get(operation_entity)
        if state is None:
            return

        raw_options = state.attributes.get("options")
        options = raw_options if isinstance(raw_options, (list, tuple, set)) else []
        option_lookup = {
            str(option).strip().lower().replace(" ", "_"): str(option)
            for option in options
        }
        selected_option = (
            option_lookup.get("general")
            or option_lookup.get("general_mode")
            or "general"
        )

        try:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": operation_entity, "option": selected_option},
                blocking=True,
            )
            _LOGGER.info(
                "GoodWe EMS control: restored %s=%s",
                operation_entity,
                selected_option,
            )
        except Exception as exc:
            _LOGGER.warning(
                "GoodWe EMS control could not restore %s to general mode: %s",
                operation_entity,
                exc,
            )

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set GoodWe to force charge mode."""
        if self._ems_prefix:
            if power_w <= 0:
                power_w = (self.data or {}).get("rated_power_w", 5000)
            return await self._ems_set_mode("charge_pv", power_w, fallback_option="charge_battery")
        if not self._connected:
            await self._controller.connect()
            self._connected = True
        rated = (self.data or {}).get("rated_power_w", 5000)
        pct = min(100, max(10, int((power_w / rated) * 100))) if power_w > 0 else 100
        return await self._controller.force_charge(power_pct=pct)

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Set GoodWe to force discharge mode."""
        if self._ems_prefix:
            if power_w <= 0:
                power_w = (self.data or {}).get("rated_power_w", 5000)
            return await self._ems_set_mode("sell_power", power_w, fallback_option="discharge_battery")
        if not self._connected:
            await self._controller.connect()
            self._connected = True
        rated = (self.data or {}).get("rated_power_w", 5000)
        pct = min(100, max(10, int((power_w / rated) * 100))) if power_w > 0 else 100
        return await self._controller.force_discharge(power_pct=pct, soc_floor=self._discharge_floor_pct)

    async def restore_normal(self) -> bool:
        """Restore GoodWe to normal operation."""
        if self._ems_prefix:
            return await self._ems_set_mode(
                "auto",
                0,
                reset_power_limit=True,
                restore_operation_mode=True,
            )
        if not self._connected:
            await self._controller.connect()
            self._connected = True
        return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        """Set minimum SOC (backup reserve) via DOD."""
        if not self._connected:
            await self._controller.connect()
            self._connected = True
        self._discharge_floor_pct = max(10, percent)
        return await self._controller.set_backup_reserve(percent)

    async def async_shutdown(self) -> None:
        """Disconnect from GoodWe system on shutdown."""
        if self._using_entity_telemetry:
            await self._telemetry_controller.disconnect()
        await self._controller.disconnect()
        self._connected = False


class SolaxBatteryEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for Solax Hybrid via the wills106/homeassistant-solax-modbus integration.

    Reads entity states published by the solax_modbus integration and assembles
    the standard PowerSync data dict. Control (force_charge, restore_normal, etc.)
    is delegated to SolaxBatteryController which writes via HA service calls.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        solax_entry_id: str | None = None,
        entity_prefix: str = "solax",
        battery_nominal_v: float = 51.2,
        max_charge_current_a: float = 25.0,
        max_discharge_current_a: float = 25.0,
        entry_id: str = "",
    ) -> None:
        from .inverters.solax_battery import SolaxBatteryController

        self._entry_id = entry_id
        self._controller = SolaxBatteryController(
            hass,
            solax_entry_id=solax_entry_id,
            entity_prefix=entity_prefix,
            battery_nominal_v=battery_nominal_v,
            max_charge_current_a=max_charge_current_a,
            max_discharge_current_a=max_discharge_current_a,
        )
        self._energy_acc = EnergyAccumulator(hass, "solax")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solax_energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return Solax data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        try:
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning("Solax entity read failed, returning stale data: %s", exc)
                return self.data
            raise UpdateFailed(f"Solax entity read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level", 0.0) or 0.0

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)
        energy_summary = self._energy_acc.as_dict()
        for status_key, summary_key in (
            ("daily_solar_energy_kwh", "pv_today_kwh"),
            ("daily_grid_import_kwh", "grid_import_today_kwh"),
            ("daily_grid_export_kwh", "grid_export_today_kwh"),
            ("daily_battery_charge_kwh", "charge_today_kwh"),
            ("daily_battery_discharge_kwh", "discharge_today_kwh"),
        ):
            value = status.get(status_key)
            if isinstance(value, (int, float)) and value >= 0:
                energy_summary[summary_key] = round(float(value), 3)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "battery_temperature": status.get("battery_temperature"),
            "pv1_power": status.get("pv1_power"),
            "pv2_power": status.get("pv2_power"),
            "pv3_power": status.get("pv3_power"),
            "pv1_voltage": status.get("pv1_voltage"),
            "pv2_voltage": status.get("pv2_voltage"),
            "pv3_voltage": status.get("pv3_voltage"),
            "pv1_current": status.get("pv1_current"),
            "pv2_current": status.get("pv2_current"),
            "pv3_current": status.get("pv3_current"),
            "mode": status.get("mode"),
            "backup_reserve": status.get("backup_reserve"),
            "min_soc": status.get("min_soc"),
            "energy_summary": energy_summary,
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._controller.set_backup_reserve(percent)

    async def set_operation_mode(self, mode: str) -> bool:
        return await self._controller.set_operation_mode(mode)

    async def curtail(self, home_load_w: int | None = None) -> bool:
        return await self._controller.curtail(home_load_w)

    async def restore_curtailment(self) -> bool:
        return await self._controller.restore()

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class SolarEdgeEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for SolarEdge Home battery telemetry via HA entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_prefix: str = "solaredge",
        solaredge_entry_id: str | None = None,
        entry_id: str = "",
    ) -> None:
        from .inverters.solaredge import SolarEdgeEnergyController

        self._entry_id = entry_id
        self._controller = SolarEdgeEnergyController(
            hass,
            entity_prefix=entity_prefix,
            solaredge_entry_id=solaredge_entry_id,
        )
        self._energy_acc = EnergyAccumulator(hass, "solaredge")
        self._daily_total_store = Store(
            hass,
            SOLAREDGE_DAILY_TOTALS_STORE_VERSION,
            f"power_sync.solaredge_daily_totals.{entry_id or entity_prefix or 'default'}",
        )
        self._daily_total_baselines_restored = False
        self._daily_total_baseline_date: str | None = None
        self._daily_total_import_baseline: float | None = None
        self._daily_total_export_baseline: float | None = None
        self._daily_total_recorder_baselines_checked = False
        self._validated = False

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solaredge_energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return SolarEdge data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        try:
            if not self._validated:
                await self._controller.connect()
                self._validated = True
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning(
                    "SolarEdge entity bridge read failed, returning stale data: %s",
                    exc,
                )
                return self.data
            raise UpdateFailed(f"SolarEdge entity bridge read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level")
        ev_power_kw = status.get("ev_power")

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)
        energy_summary = self._energy_acc.as_dict()
        await self._apply_daily_total_deltas(status, energy_summary)
        for status_key, summary_key in (
            ("daily_solar_energy_kwh", "pv_today_kwh"),
            ("daily_grid_import_kwh", "grid_import_today_kwh"),
            ("daily_grid_export_kwh", "grid_export_today_kwh"),
            ("daily_battery_charge_kwh", "charge_today_kwh"),
            ("daily_battery_discharge_kwh", "discharge_today_kwh"),
        ):
            value = status.get(status_key)
            if isinstance(value, (int, float)) and value >= 0:
                energy_summary[summary_key] = round(float(value), 3)

        data = {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "ev_power": ev_power_kw,
            "ev_power_kw": ev_power_kw,
            "ev_charger_type": "solaredge" if ev_power_kw is not None else None,
            "ev_charger_connected": ev_power_kw is not None and ev_power_kw > 0.05,
            "ev_charger_charging": ev_power_kw is not None and ev_power_kw > 0.05,
            "ev_charger_discharging": False,
            "last_update": dt_util.utcnow(),
            "battery_temperature": status.get("battery_temperature"),
            "battery_soh": status.get("battery_soh"),
            "backup_reserve": status.get("backup_reserve"),
            "min_soc": status.get("min_soc"),
            "control_available": status.get("control_available", False),
            "missing_control_entities": status.get("missing_control_entities", []),
            "control_entities": status.get("control_entities", {}),
            "energy_summary": energy_summary,
        }
        for idx in range(1, 5):
            key = f"pv{idx}_power"
            if status.get(key) is not None:
                data[key] = status[key]

        _LOGGER.debug(
            "SolarEdge entity data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%s%%), load=%.2f kW",
            data["solar_power"],
            data["grid_power"],
            data["battery_power"],
            data["battery_level"],
            data["load_power"],
        )

        return data

    async def _restore_daily_total_baselines(self) -> None:
        """Restore SolarEdge lifetime-counter baselines for the current day."""
        if self._daily_total_baselines_restored:
            return
        self._daily_total_baselines_restored = True
        try:
            stored = await self._daily_total_store.async_load()
        except Exception as exc:
            _LOGGER.debug("Failed to restore SolarEdge daily total baselines: %s", exc)
            return
        if not isinstance(stored, dict):
            return
        today = dt_util.now().date().isoformat()
        if stored.get("date") != today:
            return
        self._daily_total_baseline_date = today
        self._daily_total_import_baseline = self._float_or_none(stored.get("import_baseline_kwh"))
        self._daily_total_export_baseline = self._float_or_none(stored.get("export_baseline_kwh"))

    async def _save_daily_total_baselines(self) -> None:
        """Persist SolarEdge lifetime-counter baselines."""
        try:
            await self._daily_total_store.async_save(
                {
                    "date": self._daily_total_baseline_date,
                    "import_baseline_kwh": self._daily_total_import_baseline,
                    "export_baseline_kwh": self._daily_total_export_baseline,
                }
            )
        except Exception as exc:
            _LOGGER.debug("Failed to save SolarEdge daily total baselines: %s", exc)

    async def _apply_daily_total_deltas(self, status: dict[str, Any], energy_summary: dict[str, Any]) -> None:
        """Convert SolarEdge M1 lifetime counters into current-day deltas."""
        await self._restore_daily_total_baselines()
        today = dt_util.now().date().isoformat()
        total_import = self._float_or_none(status.get("total_grid_import_kwh"))
        total_export = self._float_or_none(status.get("total_grid_export_kwh"))
        changed = False

        if self._daily_total_baseline_date != today:
            self._daily_total_baseline_date = today
            self._daily_total_recorder_baselines_checked = False
            self._daily_total_import_baseline = await self._recorder_daily_total_baseline(
                status.get("total_grid_import_entity_id"),
                total_import,
            )
            self._daily_total_export_baseline = await self._recorder_daily_total_baseline(
                status.get("total_grid_export_entity_id"),
                total_export,
            )
            if self._daily_total_import_baseline is None:
                self._daily_total_import_baseline = total_import
            if self._daily_total_export_baseline is None:
                self._daily_total_export_baseline = total_export
            self._daily_total_recorder_baselines_checked = True
            changed = total_import is not None or total_export is not None
            if changed:
                _LOGGER.info(
                    "SolarEdge daily import/export baseline reset: import=%.3f export=%.3f kWh",
                    total_import or 0.0,
                    total_export or 0.0,
                )
        else:
            if self._daily_total_import_baseline is None and total_import is not None:
                self._daily_total_import_baseline = await self._recorder_daily_total_baseline(
                    status.get("total_grid_import_entity_id"),
                    total_import,
                )
                if self._daily_total_import_baseline is None:
                    self._daily_total_import_baseline = total_import
                changed = True
            if self._daily_total_export_baseline is None and total_export is not None:
                self._daily_total_export_baseline = await self._recorder_daily_total_baseline(
                    status.get("total_grid_export_entity_id"),
                    total_export,
                )
                if self._daily_total_export_baseline is None:
                    self._daily_total_export_baseline = total_export
                changed = True

        if not self._daily_total_recorder_baselines_checked:
            changed = await self._improve_daily_total_baselines_from_recorder(
                status,
                total_import,
                total_export,
            ) or changed
            self._daily_total_recorder_baselines_checked = True

        import_delta, import_changed = self._daily_total_delta(
            total_import,
            "_daily_total_import_baseline",
        )
        export_delta, export_changed = self._daily_total_delta(
            total_export,
            "_daily_total_export_baseline",
        )
        changed = changed or import_changed or export_changed

        if import_delta is not None:
            energy_summary["grid_import_today_kwh"] = import_delta
        if export_delta is not None:
            energy_summary["grid_export_today_kwh"] = export_delta
        if changed:
            await self._save_daily_total_baselines()

    async def _improve_daily_total_baselines_from_recorder(
        self,
        status: dict[str, Any],
        total_import: float | None,
        total_export: float | None,
    ) -> bool:
        """Lower same-day baselines when recorder has a closer midnight value."""
        changed = False
        if self._daily_total_import_baseline is not None:
            baseline = await self._recorder_daily_total_baseline(
                status.get("total_grid_import_entity_id"),
                total_import,
            )
            if baseline is not None and baseline < self._daily_total_import_baseline:
                self._daily_total_import_baseline = baseline
                changed = True
        if self._daily_total_export_baseline is not None:
            baseline = await self._recorder_daily_total_baseline(
                status.get("total_grid_export_entity_id"),
                total_export,
            )
            if baseline is not None and baseline < self._daily_total_export_baseline:
                self._daily_total_export_baseline = baseline
                changed = True
        return changed

    async def _recorder_daily_total_baseline(
        self,
        entity_id: Any,
        current_total: float | None,
    ) -> float | None:
        """Return the lifetime counter value at local midnight from recorder history."""
        if not entity_id or current_total is None:
            return None
        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.history import get_significant_states

            recorder = get_instance(self.hass)
            if recorder is None:
                return None

            now = dt_util.now()
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            history_start = day_start - timedelta(days=1)
            entity_id = str(entity_id)
            history = await recorder.async_add_executor_job(
                get_significant_states,
                self.hass,
                history_start,
                now,
                [entity_id],
            )
            states = sorted(
                (history or {}).get(entity_id, []) or [],
                key=lambda state: getattr(state, "last_changed", None) or getattr(state, "last_updated", None) or day_start,
            )
            if not states:
                return None

            last_before_midnight = None
            first_after_midnight = None
            for state in states:
                state_time = getattr(state, "last_changed", None) or getattr(state, "last_updated", None)
                value = self._history_energy_kwh(state)
                if state_time is None or value is None:
                    continue
                if self._datetime_lte(state_time, day_start):
                    last_before_midnight = value
                elif first_after_midnight is None:
                    first_after_midnight = value

            baseline = last_before_midnight if last_before_midnight is not None else first_after_midnight
            if baseline is None:
                return None
            if baseline > current_total:
                return None
            _LOGGER.debug(
                "SolarEdge recorder baseline for %s: %.3f kWh (current %.3f kWh)",
                entity_id,
                baseline,
                current_total,
            )
            return baseline
        except Exception as exc:
            _LOGGER.debug("Failed to derive SolarEdge daily baseline from recorder: %s", exc)
            return None

    def _daily_total_delta(self, total: float | None, baseline_attr: str) -> tuple[float | None, bool]:
        """Return daily delta from a lifetime total, resetting if the total rolls back."""
        if total is None:
            return (None, False)
        baseline = getattr(self, baseline_attr)
        if baseline is None:
            setattr(self, baseline_attr, total)
            return (0.0, True)
        if total < baseline:
            setattr(self, baseline_attr, total)
            return (0.0, True)
        return (round(total - baseline, 3), False)

    @staticmethod
    def _history_energy_kwh(state: Any) -> float | None:
        state_value = getattr(state, "state", None)
        if state_value in ("unavailable", "unknown", None, ""):
            return None
        try:
            value = float(state_value)
        except (TypeError, ValueError):
            return None
        unit = str((getattr(state, "attributes", {}) or {}).get("unit_of_measurement", "")).lower()
        if unit == "wh":
            return value / 1000.0
        if unit == "mwh":
            return value * 1000.0
        return value

    @staticmethod
    def _datetime_lte(left: Any, right: Any) -> bool:
        try:
            return left <= right
        except TypeError:
            left_tz = getattr(left, "tzinfo", None)
            right_tz = getattr(right, "tzinfo", None)
            if left_tz is not None and right_tz is None:
                right = right.replace(tzinfo=left_tz)
            elif left_tz is None and right_tz is not None:
                left = left.replace(tzinfo=right_tz)
            return left <= right

    @staticmethod
    def _float_or_none(value: Any) -> float | None:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()

    async def force_charge(self, duration_minutes: int = 30, power_w: int = 0) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int = 30, power_w: int = 0) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_mode(self) -> bool:
        return await self._controller.set_backup_mode()

    async def restore_work_mode_from_idle(self) -> bool:
        return await self._controller.restore_work_mode_from_idle()

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._controller.set_backup_reserve(percent)

    async def get_backup_reserve(self) -> int | None:
        return await self._controller.get_backup_reserve()

    async def set_operation_mode(self, mode: str) -> bool:
        return await self._controller.set_operation_mode(mode)


class SajH2EnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for SAJ H2 / HS2 via the saj_h2_modbus integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        saj_entry_id: str,
        battery_capacity_kwh: float = 10.0,
        entry_id: str = "",
        min_soc_pct: float = 5.0,
        inverter_rated_kw: float = 10.0,
    ) -> None:
        from .inverters.saj_h2 import SajH2BatteryController

        self._entry_id = entry_id
        self._controller = SajH2BatteryController(
            hass,
            saj_entry_id=saj_entry_id,
            battery_capacity_kwh=battery_capacity_kwh,
            min_soc_pct=min_soc_pct,
            inverter_rated_kw=inverter_rated_kw,
        )
        self._energy_acc = EnergyAccumulator(hass, "saj_h2")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_saj_h2_energy",
            update_interval=timedelta(seconds=30),
        )

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        """Propagate min_soc updates from the optimizer's backup_reserve setting."""
        self._controller.set_min_soc_pct(min_soc_pct)

    async def _async_update_data(self) -> dict[str, Any]:
        """Return SAJ data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        if not self._controller._entity_map:
            self._controller._discover_entities()

        try:
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning("SAJ H2 entity read failed, returning stale data: %s", exc)
                return self.data
            raise UpdateFailed(f"SAJ H2 entity read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level", 0.0) or 0.0

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)
        energy_summary = self._energy_acc.as_dict()
        for status_key, summary_key in (
            ("daily_solar_energy_kwh", "pv_today_kwh"),
            ("daily_grid_import_kwh", "grid_import_today_kwh"),
            ("daily_grid_export_kwh", "grid_export_today_kwh"),
        ):
            value = status.get(status_key)
            if isinstance(value, (int, float)) and value >= 0:
                energy_summary[summary_key] = round(float(value), 3)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "pv1_power": status.get("pv1_power"),
            "pv2_power": status.get("pv2_power"),
            "pv3_power": status.get("pv3_power"),
            "battery_temperature": status.get("battery_temperature"),
            "battery_soh": status.get("battery_soh"),
            "battery_capacity_kwh": status.get("battery_capacity_kwh"),
            "battery_max_charge_power_w": status.get("battery_max_charge_power_w"),
            "battery_max_discharge_power_w": status.get("battery_max_discharge_power_w"),
            "app_mode": status.get("app_mode"),
            "energy_summary": energy_summary,
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_mode(self) -> bool:
        """IDLE hold — lock battery at current SOC, no discharge."""
        return await self._controller.set_idle()

    async def restore_work_mode_from_idle(self) -> bool:
        """Exit IDLE — restore full self-consumption."""
        return await self._controller.restore_normal()

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class FroniusReservaEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for Fronius GEN24 storage via the fronius_modbus integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        fronius_entry_id: str,
        battery_capacity_kwh: float = 9.6,
        entry_id: str = "",
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        from .inverters.fronius_reserva import FroniusReservaBatteryController

        self._entry_id = entry_id
        self._controller = FroniusReservaBatteryController(
            hass,
            fronius_entry_id=fronius_entry_id,
            battery_capacity_kwh=battery_capacity_kwh,
            max_charge_kw=max_charge_kw,
            max_discharge_kw=max_discharge_kw,
        )
        self._energy_acc = EnergyAccumulator(hass, "fronius_reserva")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_fronius_reserva_energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return Fronius GEN24 storage data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        if not self._controller._entity_map:
            self._controller._discover_entities()

        try:
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning("Fronius GEN24 storage entity read failed, returning stale data: %s", exc)
                return self.data
            raise UpdateFailed(f"Fronius GEN24 storage entity read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level")
        if soc is None and self.data:
            soc = self.data.get("battery_level")
            if soc is not None:
                _LOGGER.warning(
                    "Fronius GEN24 storage SOC unavailable; using previous %.1f%% reading",
                    soc,
                )

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "battery_temperature": status.get("battery_temperature"),
            "battery_capacity_kwh": status.get("battery_capacity_kwh"),
            "battery_max_charge_power_w": status.get("battery_max_charge_power_w"),
            "battery_max_discharge_power_w": status.get("battery_max_discharge_power_w"),
            "battery_max_charge_power": (
                status.get("battery_max_charge_power_w") / 1000.0
                if status.get("battery_max_charge_power_w") else None
            ),
            "battery_max_discharge_power": (
                status.get("battery_max_discharge_power_w") / 1000.0
                if status.get("battery_max_discharge_power_w") else None
            ),
            "backup_reserve": status.get("backup_reserve"),
            "min_soc": status.get("min_soc"),
            "mode": status.get("mode"),
            "energy_summary": self._energy_acc.as_dict(),
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._controller.set_backup_reserve(percent)

    async def get_backup_reserve(self) -> int | None:
        return await self._controller.get_backup_reserve()

    async def set_backup_mode(self) -> bool:
        """IDLE hold — lock battery at current SOC, no charge or discharge."""
        return await self._controller.set_idle()

    async def restore_work_mode_from_idle(self) -> bool:
        """Exit IDLE — restore automatic storage control."""
        return await self._controller.restore_normal()

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class NeovoltEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for Neovolt / Bytewatt via the Neovolt Modbus integration."""

    def __init__(
        self,
        hass: HomeAssistant,
        neovolt_entry_id: str | list[str],
        entry_id: str = "",
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
        min_soc_pct: float = 10.0,
        surplus_balancer_mode: str = "auto",
        soc_balance_tolerance_pct: float = 5.0,
        battery_capacities_kwh: list[float | int | str | None] | None = None,
    ) -> None:
        from .inverters.neovolt import NeovoltFleetBatteryController

        self._entry_id = entry_id
        neovolt_entry_ids = (
            [neovolt_entry_id]
            if isinstance(neovolt_entry_id, str)
            else list(neovolt_entry_id)
        )
        self._controller = NeovoltFleetBatteryController(
            hass,
            neovolt_entry_ids=neovolt_entry_ids,
            max_charge_kw=max_charge_kw,
            max_discharge_kw=max_discharge_kw,
            min_soc_pct=min_soc_pct,
            surplus_balancer_mode=surplus_balancer_mode,
            soc_balance_tolerance_pct=soc_balance_tolerance_pct,
            battery_capacities_kwh=battery_capacities_kwh,
        )
        self._energy_acc = EnergyAccumulator(hass, "neovolt")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_neovolt_energy",
            update_interval=timedelta(seconds=30),
        )

    def set_min_soc_pct(self, min_soc_pct: float) -> None:
        """Propagate min_soc updates from the optimizer backup reserve setting."""
        self._controller.set_min_soc_pct(min_soc_pct)

    async def _async_update_data(self) -> dict[str, Any]:
        """Return Neovolt data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        if hasattr(self._controller, "_entity_map") and not self._controller._entity_map:
            self._controller._discover_entities()

        try:
            status = self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning("Neovolt entity read failed, returning stale data: %s", exc)
                return self.data
            raise UpdateFailed(f"Neovolt entity read failed: {exc}") from exc

        try:
            surplus_balancer = await self._controller.balance_solar_surplus(status)
        except Exception as exc:
            _LOGGER.warning("Neovolt surplus balancer skipped: %s", exc)
            surplus_balancer = status.get("surplus_balancer", {})

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level", 0.0) or 0.0

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "battery_capacity_kwh": status.get("battery_capacity_kwh"),
            "battery_soh": status.get("battery_soh"),
            "battery_max_charge_power_w": status.get("battery_max_charge_power_w"),
            "battery_max_discharge_power_w": status.get("battery_max_discharge_power_w"),
            "neovolt_surplus_balancer": surplus_balancer,
            "energy_summary": self._energy_acc.as_dict(),
        }

    async def force_charge(
        self,
        duration_minutes: int,
        power_w: int,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
        return await self._controller.force_charge(
            duration_minutes,
            power_w,
            preserve_restore_modes=preserve_restore_modes,
        )

    async def force_discharge(
        self,
        duration_minutes: int,
        power_w: int,
        *,
        preserve_restore_modes: bool = False,
    ) -> bool:
        return await self._controller.force_discharge(
            duration_minutes,
            power_w,
            preserve_restore_modes=preserve_restore_modes,
        )

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        return await self._controller.set_backup_reserve(percent)

    async def set_backup_mode(self) -> bool:
        return await self._controller.set_idle()

    async def restore_work_mode_from_idle(self) -> bool:
        return await self._controller.restore_normal()

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class AnkerSolixEnergyCoordinator(DataUpdateCoordinator):
    """Coordinator for Anker Solix direct Modbus or HA entity bridge."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        entry_id: str = "",
        connection_type: str = "modbus",
        host: str | None = None,
        port: int = 502,
        slave_id: int = 1,
        integration_domain: str = "anker_solix_official",
        anker_entry_id: str | None = None,
        entity_prefix: str | None = None,
        battery_capacity_kwh: float | None = None,
        max_charge_kw: float = 5.0,
        max_discharge_kw: float = 5.0,
    ) -> None:
        from .const import ANKER_SOLIX_CONNECTION_MODBUS
        from .inverters.anker_solix import (
            AnkerSolixEntityController,
            AnkerSolixX1ModbusController,
        )

        self._entry_id = entry_id
        self.connection_type = connection_type
        if connection_type == ANKER_SOLIX_CONNECTION_MODBUS:
            self._controller = AnkerSolixX1ModbusController(
                host=host or "",
                port=port,
                slave_id=slave_id,
                battery_capacity_kwh=battery_capacity_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
            )
        else:
            self._controller = AnkerSolixEntityController(
                hass,
                integration_domain=integration_domain,
                config_entry_id=anker_entry_id,
                entity_prefix=entity_prefix,
                battery_capacity_kwh=battery_capacity_kwh,
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
            )
        self._energy_acc = EnergyAccumulator(hass, "anker_solix")

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_anker_solix_energy",
            update_interval=timedelta(seconds=30),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return Anker Solix data from direct Modbus or HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        try:
            status = await self._controller.get_status() if asyncio.iscoroutinefunction(self._controller.get_status) else self._controller.get_status()
        except Exception as exc:
            if self.data:
                _LOGGER.warning("Anker Solix read failed, returning stale data: %s", exc)
                return self.data
            raise UpdateFailed(f"Anker Solix read failed: {exc}") from exc

        solar_kw = status.get("solar_power", 0.0) or 0.0
        grid_kw = status.get("grid_power", 0.0) or 0.0
        battery_kw = status.get("battery_power", 0.0) or 0.0
        load_kw = status.get("load_power", 0.0) or 0.0
        soc = status.get("battery_level", 0.0) or 0.0

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": soc,
            "battery_capacity_kwh": status.get("battery_capacity_kwh"),
            "battery_max_charge_power_w": status.get("battery_max_charge_power_w"),
            "battery_max_discharge_power_w": status.get("battery_max_discharge_power_w"),
            "battery_status": status.get("battery_status"),
            "operating_mode": status.get("operating_mode") or status.get("mode"),
            "control_path": status.get("control_path"),
            "dispatch_supported": status.get("dispatch_supported", True),
            "energy_summary": self._energy_acc.as_dict(),
        }

    async def force_charge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_charge(duration_minutes, power_w)

    async def force_discharge(self, duration_minutes: int, power_w: int) -> bool:
        return await self._controller.force_discharge(duration_minutes, power_w)

    async def restore_normal(self) -> bool:
        return await self._controller.restore_normal()

    async def set_self_consumption_mode(self) -> bool:
        if hasattr(self._controller, "set_self_consumption_mode"):
            return await self._controller.set_self_consumption_mode()
        return await self.restore_normal()

    async def set_backup_mode(self) -> bool:
        if hasattr(self._controller, "set_backup_mode"):
            return await self._controller.set_backup_mode()
        return False

    async def restore_work_mode_from_idle(self) -> bool:
        if hasattr(self._controller, "restore_work_mode_from_idle"):
            return await self._controller.restore_work_mode_from_idle()
        return await self.restore_normal()

    async def set_backup_reserve(self, percent: int) -> bool:
        if hasattr(self._controller, "set_backup_reserve"):
            return await self._controller.set_backup_reserve(percent)
        return False

    async def get_backup_reserve(self) -> int | None:
        if hasattr(self._controller, "get_backup_reserve"):
            return await self._controller.get_backup_reserve()
        return None

    async def async_shutdown(self) -> None:
        await self._controller.disconnect()


class ESYSunhomeEnergyCoordinator(DataUpdateCoordinator):
    """Bridge coordinator for ESY Sunhome via the upstream esy_sunhome integration.

    Reads entity states published by the esy_sunhome integration (which handles the
    ESY cloud MQTT connection) and assembles the standard PowerSync data dict.
    Control commands are sent via HA's select.select_option service on the ESY
    mode-select entity (Regular Mode / Emergency Mode / Electricity Sell Mode).

    W-level charge/discharge setpoints are not supported by ESY Sunhome hardware;
    force_charge/force_discharge map to coarse mode switches only.
    """

    ESY_DOMAIN = "esy_sunhome"

    # Maps ESY sensor translation_key → internal slot name
    _SENSOR_KEYS = {
        "batterySoc": "battery_soc",
        "pvPower": "pv_w",
        "gridPower": "grid_w",
        "loadPower": "load_w",
        "batteryImport": "battery_import_w",
        "batteryExport": "battery_export_w",
        "batteryPower": "battery_abs_w",
        "ratedPower": "rated_w",
        "inverterTemp": "inv_temp",
        "dailyPowerGeneration": "daily_gen_kwh",
        "dailyPowerConsumption": "daily_load_kwh",
        "dailyBattCharge": "daily_charge_kwh",
        "dailyBattDischarge": "daily_discharge_kwh",
        "batteryStatusText": "battery_status_text",
        "batterySoh": "battery_soh",
    }
    _MODE_SELECT_KEY = "code"

    def __init__(
        self,
        hass: HomeAssistant,
        esy_entry_id: str,
        entry_id: str = "",
    ) -> None:
        self._esy_entry_id = esy_entry_id
        self._entry_id = entry_id
        self._entity_map: dict[str, str] = {}   # esy_key → ha entity_id
        self._mode_select_entity_id: str | None = None
        self._energy_acc = EnergyAccumulator(hass, "esy_sunhome")

        super().__init__(
            hass,
            _LOGGER,
            name="ESY Sunhome Energy",
            update_interval=timedelta(seconds=30),
        )

    def _discover_entities(self) -> None:
        """Discover esy_sunhome entities from the HA entity registry once."""
        from homeassistant.helpers import entity_registry as er

        esy_entry = self.hass.config_entries.async_get_entry(self._esy_entry_id)
        if not esy_entry:
            _LOGGER.warning("ESY Sunhome config entry %s not found", self._esy_entry_id)
            return

        # device_id in ESY config entry is the numeric cloud device ID, used as
        # the unique_id prefix: "{device_id}_{translation_key}"
        device_id = esy_entry.data.get("device_id", "")
        if not device_id:
            _LOGGER.warning("ESY Sunhome config entry missing device_id")
            return

        registry = er.async_get(self.hass)
        uid_to_eid: dict[str, str] = {
            reg_entry.unique_id: reg_entry.entity_id
            for reg_entry in er.async_entries_for_config_entry(registry, self._esy_entry_id)
            if reg_entry.unique_id
        }

        for esy_key in self._SENSOR_KEYS:
            uid = f"{device_id}_{esy_key}"
            if uid in uid_to_eid:
                self._entity_map[esy_key] = uid_to_eid[uid]

        mode_uid = f"{device_id}_{self._MODE_SELECT_KEY}"
        self._mode_select_entity_id = uid_to_eid.get(mode_uid)

        _LOGGER.info(
            "ESY Sunhome entity discovery: %d/%d sensors found, mode_select=%s",
            len(self._entity_map), len(self._SENSOR_KEYS), self._mode_select_entity_id,
        )

    def _state_float(self, esy_key: str, default: float | None = None) -> float | None:
        entity_id = self._entity_map.get(esy_key)
        if not entity_id:
            return default
        state = self.hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", ""):
            return default
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return default

    async def _async_update_data(self) -> dict[str, Any]:
        """Return ESY Sunhome data assembled from HA entity states."""
        if not self._energy_acc._last_update:
            await self._energy_acc.async_restore()

        if not self._entity_map:
            self._discover_entities()

        if not self._entity_map:
            if self.data:
                _LOGGER.warning("ESY Sunhome: entity map empty, returning stale data")
                return self.data
            raise UpdateFailed("ESY Sunhome entities not yet available — is esy_sunhome integration running?")

        pv_w = self._state_float("pvPower", 0.0) or 0.0
        grid_w = self._state_float("gridPower", 0.0) or 0.0   # positive = import (already HA convention)
        load_w = self._state_float("loadPower", 0.0) or 0.0
        battery_import_w = self._state_float("batteryImport")
        battery_export_w = self._state_float("batteryExport")
        battery_abs_w = self._state_float("batteryPower", 0.0) or 0.0

        # Signed battery power: positive = discharging, negative = charging
        if battery_import_w is not None or battery_export_w is not None:
            battery_w = (battery_export_w or 0.0) - (battery_import_w or 0.0)
        else:
            battery_w = battery_abs_w  # unsigned fallback; direction unknown

        solar_kw = pv_w / 1000.0
        grid_kw = grid_w / 1000.0
        battery_kw = battery_w / 1000.0
        load_kw = load_w / 1000.0
        battery_level = self._state_float("batterySoc")

        rated_w = self._state_float("ratedPower", 5000.0) or 5000.0

        work_mode_name = None
        if self._mode_select_entity_id:
            ms = self.hass.states.get(self._mode_select_entity_id)
            if ms and ms.state not in ("unavailable", "unknown"):
                work_mode_name = ms.state

        buy, sell = _get_current_prices(self.hass, self._entry_id)
        self._energy_acc.update(max(0.0, solar_kw), grid_kw, battery_kw, load_kw, buy, sell)

        _LOGGER.debug(
            "ESY Sunhome data: solar=%.2f kW, grid=%.2f kW, battery=%.2f kW (%.0f%%), load=%.2f kW",
            solar_kw, grid_kw, battery_kw, battery_level or 0.0, load_kw,
        )

        return {
            "solar_power": solar_kw,
            "grid_power": grid_kw,
            "battery_power": battery_kw,
            "load_power": load_kw,
            "battery_level": battery_level,
            "last_update": dt_util.utcnow(),
            "work_mode": work_mode_name,
            "work_mode_name": work_mode_name,
            "battery_max_charge_power_w": rated_w,
            "battery_max_discharge_power_w": rated_w,
            "battery_max_charge_power": round(rated_w / 1000.0, 2),
            "battery_max_discharge_power": round(rated_w / 1000.0, 2),
            "inverter_temperature": self._state_float("inverterTemp"),
            "battery_status_text": (
                self.hass.states.get(self._entity_map["batteryStatusText"]).state
                if "batteryStatusText" in self._entity_map
                   and self.hass.states.get(self._entity_map["batteryStatusText"]) is not None
                   and self.hass.states.get(self._entity_map["batteryStatusText"]).state
                      not in ("unavailable", "unknown")
                else None
            ),
            "battery_soh": self._state_float("batterySoh"),
            "daily_generation_kwh": self._state_float("dailyPowerGeneration"),
            "daily_consumption_kwh": self._state_float("dailyPowerConsumption"),
            "daily_battery_charge_kwh": self._state_float("dailyBattCharge"),
            "daily_battery_discharge_kwh": self._state_float("dailyBattDischarge"),
            "energy_summary": self._energy_acc.as_dict(),
        }

    async def _set_mode(self, option: str) -> bool:
        """Switch the ESY operating mode via its mode-select entity."""
        if not self._mode_select_entity_id:
            self._discover_entities()
        if not self._mode_select_entity_id:
            _LOGGER.error("ESY Sunhome: mode select entity not found — cannot change mode")
            return False
        try:
            await self.hass.services.async_call(
                "select", "select_option",
                {"entity_id": self._mode_select_entity_id, "option": option},
                blocking=True,
            )
            _LOGGER.info("ESY Sunhome: set mode → '%s'", option)
            return True
        except Exception as exc:
            _LOGGER.error("ESY Sunhome: failed to set mode '%s': %s", option, exc)
            return False

    async def force_charge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force grid-charge via Emergency Mode (rate is inverter-decided)."""
        return await self._set_mode("Emergency Mode")

    async def force_discharge(self, duration_minutes: int = 30, power_w: float = 0) -> bool:
        """Force grid-export via Electricity Sell Mode (rate is inverter-decided)."""
        return await self._set_mode("Electricity Sell Mode")

    async def restore_normal(self) -> bool:
        """Return to Regular Mode (self-consumption)."""
        return await self._set_mode("Regular Mode")

    async def set_backup_reserve(self, percent: int) -> bool:
        _LOGGER.info("ESY Sunhome: set_backup_reserve not supported on this hardware")
        return True

    async def set_self_consumption_mode(self) -> bool:
        return await self._set_mode("Regular Mode")

    async def set_autonomous_mode(self) -> bool:
        return await self._set_mode("Regular Mode")

    async def set_work_mode(self, mode: str) -> bool:
        _mode_map = {
            "self_consumption": "Regular Mode",
            "regular": "Regular Mode",
            "feed_in": "Electricity Sell Mode",
            "electricity_sell": "Electricity Sell Mode",
            "backup": "Emergency Mode",
            "emergency": "Emergency Mode",
        }
        return await self._set_mode(_mode_map.get(mode.lower(), "Regular Mode"))

    async def restore_work_mode_from_idle(self) -> bool:
        return await self._set_mode("Regular Mode")

    async def set_charge_rate_limit(self, amps: float) -> bool:
        _LOGGER.info("ESY Sunhome: set_charge_rate_limit not supported on this hardware")
        return True

    async def set_discharge_rate_limit(self, amps: float) -> bool:
        _LOGGER.info("ESY Sunhome: set_discharge_rate_limit not supported on this hardware")
        return True

    async def async_shutdown(self) -> None:
        pass


class SolcastForecastCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Solcast solar production forecasts.

    Fetches PV power forecasts from Solcast API and caches them locally.
    Dynamically adjusts update interval based on number of resource IDs to stay
    within Solcast's 10 calls/day hobbyist tier limit.

    Supports multiple resource IDs for split arrays (e.g., east/west facing panels).
    Provide comma-separated resource IDs and forecasts will be combined by summing values.
    """

    # Solcast API base URL
    SOLCAST_API_URL = "https://api.solcast.com.au"

    # Solcast hobbyist tier: 10 API calls per day
    DAILY_API_LIMIT = 10

    def __init__(
        self,
        hass: HomeAssistant,
        api_key: str,
        resource_id: str,
        capacity_kw: float | None = None,
        estimate_type: str = DEFAULT_SOLCAST_ESTIMATE_TYPE,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            api_key: Solcast API key
            resource_id: Rooftop site resource ID(s) - comma-separated for split arrays
            capacity_kw: System capacity in kW (optional, for validation)
            estimate_type: Solcast estimate to use: estimate, estimate10, or estimate90
        """
        self._api_key = api_key
        self._estimate_type = (
            estimate_type
            if estimate_type in _SOLCAST_ESTIMATE_FIELDS
            else DEFAULT_SOLCAST_ESTIMATE_TYPE
        )
        # Support comma-separated resource IDs for split arrays
        self._resource_ids = [rid.strip() for rid in resource_id.split(",") if rid.strip()]
        self._capacity_kw = capacity_kw
        self._session = async_get_clientsession(hass)

        # Cache for full-day forecast (stored on first fetch of the day)
        self._daily_forecast_date: str | None = None  # Date string (YYYY-MM-DD)
        self._daily_forecast_kwh: float | None = None  # Full day's forecast
        self._daily_forecast_peak_kw: float | None = None  # Peak for the day

        # Rate limiting tracking (persisted to survive restarts)
        self._rate_limited = False
        self._last_rate_limit_time: datetime | None = None
        self._api_calls_today = 0
        self._api_calls_date: str | None = None
        self._rate_limit_store = Store(hass, 1, f"{DOMAIN}_solcast_rate_limit")
        self._forecast_store = Store(hass, 1, f"{DOMAIN}_solcast_forecast_cache")

        # Calculate update interval based on number of resources
        # Each resource requires 1 API call per update
        # With 10 calls/day limit: interval = 24 / (10 / n_resources) hours
        n_resources = len(self._resource_ids)
        calls_per_update = n_resources  # We skip estimated_actuals to save calls
        max_updates_per_day = self.DAILY_API_LIMIT // calls_per_update
        # Leave some buffer - aim for 80% of max to avoid hitting limit
        safe_updates = max(1, int(max_updates_per_day * 0.8))
        update_hours = max(3, 24 // safe_updates)  # Minimum 3 hours

        self._update_interval = timedelta(hours=update_hours)

        _LOGGER.info(
            f"Solcast coordinator: {n_resources} resource(s), "
            f"{calls_per_update} API call(s)/update, "
            f"update interval: {update_hours}h ({safe_updates} updates/day), "
            f"estimate_type={self._estimate_type}"
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_solcast_forecast",
            update_interval=self._update_interval,
        )

    def _get_pv_estimate(self, period: dict[str, Any]) -> float:
        """Return the configured Solcast estimate value for a forecast period."""
        for field in _SOLCAST_ESTIMATE_FIELDS[self._estimate_type]:
            value = period.get(field)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _find_solcast_sensor(self, patterns: list[str]) -> Any | None:
        """Find a Solcast sensor by trying multiple possible entity ID patterns."""
        for pattern in patterns:
            state = self.hass.states.get(pattern)
            if state and state.state not in ("unavailable", "unknown", None, ""):
                return state
        return None

    async def _try_read_from_solcast_integration(self) -> dict[str, Any] | None:
        """Try to read forecast data from the Solcast HA integration.

        If the Solcast integration is installed, we read from its sensors instead
        of making our own API calls. This avoids doubling API usage (10 calls/day limit).

        Supports multiple naming conventions:
        - sensor.solcast_pv_forecast_* (current Solcast integration)
        - sensor.solcast_forecast_* (alternative naming)
        - sensor.solcast_* (older versions)

        Returns:
            Forecast data dict if Solcast integration is available, None otherwise
        """
        try:
            # Try multiple possible sensor names for today's forecast
            today_patterns = [
                "sensor.solcast_pv_forecast_forecast_today",
                "sensor.solcast_forecast_today",
                "sensor.solcast_pv_forecast_today",
            ]
            today_state = self._find_solcast_sensor(today_patterns)
            if not today_state:
                return None

            # Get all the sensor values - try multiple naming patterns
            tomorrow_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_tomorrow",
                "sensor.solcast_forecast_tomorrow",
                "sensor.solcast_pv_forecast_tomorrow",
            ])
            remaining_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_remaining_today",
                "sensor.solcast_forecast_remaining_today",
                "sensor.solcast_pv_forecast_remaining_today",
            ])
            peak_today_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_peak_forecast_today",
                "sensor.solcast_peak_forecast_today",
                "sensor.solcast_pv_forecast_peak_today",
            ])
            peak_tomorrow_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_peak_forecast_tomorrow",
                "sensor.solcast_peak_forecast_tomorrow",
                "sensor.solcast_pv_forecast_peak_tomorrow",
            ])
            power_now_state = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_power_now",
                "sensor.solcast_power_now",
                "sensor.solcast_pv_forecast_now",
            ])

            # Parse values - these are already in kWh
            today_forecast = float(today_state.state) if today_state.state else 0
            tomorrow_forecast = float(tomorrow_state.state) if tomorrow_state and tomorrow_state.state not in ("unavailable", "unknown", None, "") else 0
            remaining = float(remaining_state.state) if remaining_state and remaining_state.state not in ("unavailable", "unknown", None, "") else today_forecast

            # Peak values are in W - convert to kW
            today_peak = None
            if peak_today_state and peak_today_state.state not in ("unavailable", "unknown", None, ""):
                today_peak = float(peak_today_state.state) / 1000.0  # W to kW

            tomorrow_peak = None
            if peak_tomorrow_state and peak_tomorrow_state.state not in ("unavailable", "unknown", None, ""):
                tomorrow_peak = float(peak_tomorrow_state.state) / 1000.0  # W to kW

            # Current power estimate is in W - convert to kW
            current_estimate = None
            if power_now_state and power_now_state.state not in ("unavailable", "unknown", None, ""):
                current_estimate = float(power_now_state.state) / 1000.0  # W to kW

            # Try to get detailed hourly forecast from sensor attributes
            # The Solcast HA integration stores this in various attribute names
            detailed_forecast = None
            if today_state.attributes:
                # Try common attribute names used by Solcast HA integration
                detailed_forecast = (
                    today_state.attributes.get("detailedForecast") or
                    today_state.attributes.get("forecast_today") or
                    today_state.attributes.get("detailedHourly") or
                    today_state.attributes.get("forecasts")
                )

            # Build hourly forecast data for chart overlay
            hourly_forecast = []
            if detailed_forecast and isinstance(detailed_forecast, list):
                now = dt_util.now()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)

                for period in detailed_forecast:
                    try:
                        # Parse period end time and the configured estimate field.
                        period_end_str = period.get("period_end", "")
                        pv_estimate = self._get_pv_estimate(period)

                        if period_end_str:
                            period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                            period_local = dt_util.as_local(period_end)

                            # Only include today's data for the chart
                            if today_start <= period_local <= today_end:
                                hourly_forecast.append({
                                    "time": period_local.strftime("%H:%M"),
                                    "hour": period_local.hour,
                                    "pv_estimate_kw": round(pv_estimate, 2),
                                })
                    except (ValueError, TypeError, KeyError):
                        continue

            # Try to also get tomorrow's detailed forecast for optimizer (48h horizon)
            # Check the tomorrow forecast sensor for detailed data
            tomorrow_detailed = None
            tomorrow_state_obj = self._find_solcast_sensor([
                "sensor.solcast_pv_forecast_forecast_tomorrow",
                "sensor.solcast_forecast_tomorrow",
                "sensor.solcast_pv_forecast_tomorrow",
            ])
            if tomorrow_state_obj and tomorrow_state_obj.attributes:
                tomorrow_detailed = (
                    tomorrow_state_obj.attributes.get("detailedForecast") or
                    tomorrow_state_obj.attributes.get("forecast_tomorrow") or
                    tomorrow_state_obj.attributes.get("detailedHourly") or
                    tomorrow_state_obj.attributes.get("forecasts")
                )

            # Combine today and tomorrow forecasts for optimizer
            full_forecasts = []
            if detailed_forecast and isinstance(detailed_forecast, list):
                full_forecasts.extend(detailed_forecast)
            if tomorrow_detailed and isinstance(tomorrow_detailed, list):
                full_forecasts.extend(tomorrow_detailed)

            if full_forecasts:
                selected_today = 0.0
                selected_remaining = 0.0
                selected_tomorrow = 0.0
                selected_today_peak = 0.0
                selected_tomorrow_peak = 0.0
                selected_current: float | None = None
                has_today_period = False
                has_tomorrow_period = False
                now = dt_util.now()
                today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
                tomorrow_end = today_end + timedelta(days=1)
                period_hours = 0.5

                for period in full_forecasts:
                    if not isinstance(period, dict):
                        continue
                    period_end_str = period.get("period_end") or period.get("period")
                    period_start_str = period.get("period_start")
                    if not period_end_str and not period_start_str:
                        continue
                    try:
                        if period_end_str:
                            period_end = (
                                period_end_str
                                if isinstance(period_end_str, datetime)
                                else datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                            )
                        else:
                            period_start = (
                                period_start_str
                                if isinstance(period_start_str, datetime)
                                else datetime.fromisoformat(period_start_str.replace("Z", "+00:00"))
                            )
                            period_end = period_start + timedelta(minutes=30)
                        period_local = dt_util.as_local(period_end)
                        pv_estimate = self._get_pv_estimate(period)

                        if selected_current is None and period_local >= now:
                            selected_current = pv_estimate
                        if today_start <= period_local <= today_end:
                            has_today_period = True
                            selected_today += pv_estimate * period_hours
                            selected_today_peak = max(selected_today_peak, pv_estimate)
                            if period_local >= now:
                                selected_remaining += pv_estimate * period_hours
                        elif today_end < period_local <= tomorrow_end:
                            has_tomorrow_period = True
                            selected_tomorrow += pv_estimate * period_hours
                            selected_tomorrow_peak = max(selected_tomorrow_peak, pv_estimate)
                    except (ValueError, TypeError, KeyError):
                        continue

                if has_today_period:
                    today_forecast = selected_today
                    remaining = selected_remaining
                    today_peak = selected_today_peak
                if has_tomorrow_period:
                    tomorrow_forecast = selected_tomorrow
                    tomorrow_peak = selected_tomorrow_peak
                if selected_current is not None:
                    current_estimate = selected_current

            _LOGGER.info(
                f"Solcast (from HA integration): Today={today_forecast:.1f}kWh, "
                f"remaining={remaining:.1f}kWh, Tomorrow={tomorrow_forecast:.1f}kWh, "
                f"hourly_points={len(hourly_forecast)}, raw_periods={len(full_forecasts)}, "
                f"estimate_type={self._estimate_type}"
            )

            return {
                "available": True,
                "today_forecast_kwh": round(today_forecast, 2),
                "today_remaining_kwh": round(remaining, 2),
                "today_total_kwh": round(today_forecast, 2),
                "tomorrow_total_kwh": round(tomorrow_forecast, 2),
                "today_peak_kw": round(today_peak, 2) if today_peak else None,
                "tomorrow_peak_kw": round(tomorrow_peak, 2) if tomorrow_peak else None,
                "current_estimate_kw": round(current_estimate, 2) if current_estimate else None,
                "hourly_forecast": hourly_forecast,  # For chart overlay
                "forecasts": full_forecasts if full_forecasts else None,  # Raw periods for optimizer
                "estimate_type": self._estimate_type,
                "forecast_periods": len(full_forecasts) if full_forecasts else len(hourly_forecast),
                "last_update": dt_util.utcnow(),
                "source": "solcast_integration",
            }

        except (ValueError, TypeError, AttributeError) as e:
            _LOGGER.debug(f"Could not read from Solcast integration: {e}")
            return None

    async def _fetch_forecast_for_resource(self, resource_id: str) -> list[dict] | None:
        """Fetch forecast for a single resource ID.

        Args:
            resource_id: Solcast rooftop site resource ID

        Returns:
            List of forecast periods or None on error
        """
        url = f"{self.SOLCAST_API_URL}/rooftop_sites/{resource_id}/forecasts"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        params = {"hours": 48, "format": "json"}

        async with self._session.get(url, headers=headers, params=params) as response:
            if response.status == 401:
                # Most common cause: user pasted a stale/rotated API key, or
                # the resource_id belongs to a different Solcast account than
                # the API key does. Surface key prefix + resource so the user
                # can at least tell that the right values reached the API.
                key_preview = (
                    f"{self._api_key[:4]}…{self._api_key[-4:]}"
                    if len(self._api_key) > 8 else "<short>"
                )
                raise UpdateFailed(
                    "Solcast API 401 Unauthorized — API key does not match an "
                    "active account, or resource_id belongs to a different "
                    "account. Verify both at toolkit.solcast.com.au → API "
                    f"Management. (key={key_preview}, resource={resource_id})"
                )
            if response.status == 429:
                self._rate_limited = True
                self._last_rate_limit_time = dt_util.now()
                # Trust the server — our counter may be wrong (e.g. calls from
                # another session or before counter was persisted)
                if self._api_calls_today < self.DAILY_API_LIMIT:
                    _LOGGER.warning(
                        f"Solcast 429 but counter shows {self._api_calls_today}/{self.DAILY_API_LIMIT} — "
                        f"syncing counter to server reality"
                    )
                    self._api_calls_today = self.DAILY_API_LIMIT
                    self.hass.async_create_task(
                        self._rate_limit_store.async_save({
                            "date": dt_util.utcnow().strftime("%Y-%m-%d"),
                            "calls": self._api_calls_today,
                        })
                    )
                _LOGGER.warning(
                    f"Solcast API rate limit hit for resource {resource_id[:8]}... "
                    f"(API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}). "
                    f"Will use cached data until tomorrow."
                )
                return None
            if response.status != 200:
                _LOGGER.error(f"Solcast API error for resource {resource_id[:8]}: {response.status}")
                return None

            data = await response.json()
            return data.get("forecasts", [])

    async def _fetch_estimated_actuals_for_resource(self, resource_id: str) -> list[dict] | None:
        """Fetch estimated actuals (past production) for a single resource ID.

        Args:
            resource_id: Solcast rooftop site resource ID

        Returns:
            List of estimated actual periods or None on error
        """
        url = f"{self.SOLCAST_API_URL}/rooftop_sites/{resource_id}/estimated_actuals"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        # Get last 24 hours of estimated actuals (covers today's past production)
        params = {"hours": 24, "format": "json"}

        try:
            async with self._session.get(url, headers=headers, params=params) as response:
                if response.status == 401:
                    _LOGGER.warning("Solcast estimated_actuals auth failed")
                    return None
                if response.status == 429:
                    _LOGGER.warning(f"Solcast API rate limit for estimated_actuals {resource_id[:8]}...")
                    return None
                if response.status != 200:
                    _LOGGER.debug(f"Solcast estimated_actuals error for {resource_id[:8]}: {response.status}")
                    return None

                data = await response.json()
                return data.get("estimated_actuals", [])
        except Exception as e:
            _LOGGER.debug(f"Error fetching estimated_actuals: {e}")
            return None

    def _combine_forecasts(self, base: list[dict], additional: list[dict]) -> list[dict]:
        """Combine forecasts from multiple resources by summing pv_estimate values.

        Args:
            base: Base forecast list
            additional: Additional forecast list to add

        Returns:
            Combined forecast list with summed values
        """
        additional_lookup = {f.get("period_end"): f for f in additional}

        combined = []
        for forecast in base:
            period_end = forecast.get("period_end")
            result = dict(forecast)

            if period_end in additional_lookup:
                add_f = additional_lookup[period_end]
                if result.get("pv_estimate") is not None and add_f.get("pv_estimate") is not None:
                    result["pv_estimate"] = result["pv_estimate"] + add_f["pv_estimate"]
                if result.get("pv_estimate10") is not None and add_f.get("pv_estimate10") is not None:
                    result["pv_estimate10"] = result["pv_estimate10"] + add_f["pv_estimate10"]
                if result.get("pv_estimate90") is not None and add_f.get("pv_estimate90") is not None:
                    result["pv_estimate90"] = result["pv_estimate90"] + add_f["pv_estimate90"]

            combined.append(result)

        return combined

    async def _restore_rate_limit_state(self) -> None:
        """Restore API call counter from persistent storage."""
        try:
            data = await self._rate_limit_store.async_load()
            if data:
                # Solcast resets at UTC midnight
                today_str = dt_util.utcnow().strftime("%Y-%m-%d")
                if data.get("date") == today_str:
                    self._api_calls_today = data.get("calls", 0)
                    self._api_calls_date = today_str
                    if self._api_calls_today >= self.DAILY_API_LIMIT:
                        self._rate_limited = True
                    _LOGGER.info(
                        f"Restored Solcast API call counter: {self._api_calls_today}/{self.DAILY_API_LIMIT} "
                        f"(rate_limited={self._rate_limited})"
                    )
        except Exception:
            pass

    async def _save_forecast_cache(self, data: dict[str, Any]) -> None:
        """Persist last good forecast data to survive restarts."""
        try:
            cache = {
                "date": dt_util.now().strftime("%Y-%m-%d"),
                "today_forecast_kwh": data.get("today_forecast_kwh"),
                "today_remaining_kwh": data.get("today_remaining_kwh"),
                "today_total_kwh": data.get("today_total_kwh"),
                "tomorrow_total_kwh": data.get("tomorrow_total_kwh"),
                "today_peak_kw": data.get("today_peak_kw"),
                "tomorrow_peak_kw": data.get("tomorrow_peak_kw"),
                "source": data.get("source"),
                "estimate_type": data.get("estimate_type", self._estimate_type),
                "forecasts": data.get("forecasts"),
                # Also persist the in-memory full-day forecast cache so that
                # restarting mid-day doesn't reset it and force the coordinator
                # into the "today_remaining becomes today_forecast" fallback
                # that makes the forecast sensor show partial-day numbers.
                "_daily_forecast_date": self._daily_forecast_date,
                "_daily_forecast_kwh": self._daily_forecast_kwh,
                "_daily_forecast_peak_kw": self._daily_forecast_peak_kw,
            }
            await self._forecast_store.async_save(cache)
        except Exception:
            pass

    async def _restore_daily_forecast_cache(self) -> None:
        """Restore the in-memory _daily_forecast_* fields from disk.

        Ensures that a mid-day HA restart doesn't reset the cached full-day
        forecast back to None and then overwrite it with `today_remaining`
        on the next fetch (which would make the sensor show only the
        rest-of-day forecast as if it were the full day).
        """
        try:
            cache = await self._forecast_store.async_load()
            if not cache:
                return
            cached_estimate_type = cache.get("estimate_type")
            if (
                cached_estimate_type != self._estimate_type
                and (cached_estimate_type is not None or self._estimate_type != DEFAULT_SOLCAST_ESTIMATE_TYPE)
            ):
                return
            cached_date = cache.get("_daily_forecast_date")
            if cached_date != dt_util.now().strftime("%Y-%m-%d"):
                return
            self._daily_forecast_date = cached_date
            # Prefer the explicit full-day cache if persisted; fall back to
            # today_forecast_kwh which older releases stored under that key.
            self._daily_forecast_kwh = (
                cache.get("_daily_forecast_kwh")
                if cache.get("_daily_forecast_kwh") is not None
                else cache.get("today_forecast_kwh")
            )
            self._daily_forecast_peak_kw = (
                cache.get("_daily_forecast_peak_kw")
                if cache.get("_daily_forecast_peak_kw") is not None
                else cache.get("today_peak_kw")
            )
            _LOGGER.info(
                "Solcast: restored full-day forecast cache for %s: %.1fkWh",
                self._daily_forecast_date, self._daily_forecast_kwh or 0,
            )
        except Exception:
            pass

    async def _restore_forecast_cache(self) -> dict[str, Any] | None:
        """Restore last good forecast data from persistent storage."""
        try:
            cache = await self._forecast_store.async_load()
            if cache and cache.get("date") == dt_util.now().strftime("%Y-%m-%d"):
                cached_estimate_type = cache.get("estimate_type")
                if (
                    cached_estimate_type != self._estimate_type
                    and (cached_estimate_type is not None or self._estimate_type != DEFAULT_SOLCAST_ESTIMATE_TYPE)
                ):
                    return None
                forecasts = cache.get("forecasts")
                n_periods = len(forecasts) if forecasts else 0
                _LOGGER.info(
                    f"Restored cached solar forecast: "
                    f"today={cache.get('today_forecast_kwh')}kWh, "
                    f"{n_periods} forecast periods"
                )
                return {
                    "available": True,
                    "today_forecast_kwh": cache.get("today_forecast_kwh", 0),
                    "today_remaining_kwh": cache.get("today_remaining_kwh", 0),
                    "today_total_kwh": cache.get("today_total_kwh", 0),
                    "tomorrow_total_kwh": cache.get("tomorrow_total_kwh", 0),
                    "today_peak_kw": cache.get("today_peak_kw"),
                    "tomorrow_peak_kw": cache.get("tomorrow_peak_kw"),
                    "current_estimate_kw": None,
                    "forecasts": forecasts,
                    "estimate_type": cache.get("estimate_type", self._estimate_type),
                    "forecast_periods": n_periods,
                    "last_update": dt_util.utcnow(),
                    "source": f"{cache.get('source', 'cache')}_restored",
                }
            return None
        except Exception:
            return None

    async def _restore_from_ha_state(self) -> dict[str, Any] | None:
        """Restore forecast from HA's last known sensor state or recorder history.

        First checks hass.states for a non-zero value (restored from recorder on startup).
        If that's 0 (from a previous bug), queries the recorder for the last non-zero value
        from today's history.
        """
        entity_ids = [
            "sensor.power_sync_solcast_today_forecast",
            "sensor.power_sync_solar_forecast_today",
        ]

        def _make_result(today_kwh: float, source: str) -> dict[str, Any]:
            return {
                "available": True,
                "today_forecast_kwh": today_kwh,
                "today_remaining_kwh": 0,
                "today_total_kwh": today_kwh,
                "tomorrow_total_kwh": 0,
                "today_peak_kw": None,
                "tomorrow_peak_kw": None,
                "current_estimate_kw": None,
                "forecasts": None,
                "forecast_periods": 0,
                "last_update": dt_util.utcnow(),
                "source": source,
            }

        try:
            # First: check current state (fast path)
            for entity_id in entity_ids:
                state = self.hass.states.get(entity_id)
                if state and state.state not in ("unavailable", "unknown", None, ""):
                    try:
                        today_kwh = float(state.state)
                        if today_kwh > 0:
                            _LOGGER.info(
                                f"Restored solar forecast from HA state: "
                                f"{entity_id}={today_kwh:.1f}kWh"
                            )
                            return _make_result(today_kwh, "ha_state_restored")
                    except (ValueError, TypeError):
                        continue

            # Second: query recorder history for last non-zero value today
            try:
                from homeassistant.components.recorder import get_instance
                from homeassistant.components.recorder.history import state_changes_during_period

                now = dt_util.now()
                start = now.replace(hour=0, minute=0, second=0, microsecond=0)

                for entity_id in entity_ids:
                    history = await get_instance(self.hass).async_add_executor_job(
                        state_changes_during_period,
                        self.hass,
                        start,
                        now,
                        entity_id,
                    )
                    states = history.get(entity_id, [])
                    # Walk backwards to find last non-zero value
                    for hist_state in reversed(states):
                        if hist_state.state in ("unavailable", "unknown", None, ""):
                            continue
                        try:
                            val = float(hist_state.state)
                            if val > 0:
                                _LOGGER.info(
                                    f"Restored solar forecast from recorder history: "
                                    f"{entity_id}={val:.1f}kWh (from {hist_state.last_changed})"
                                )
                                return _make_result(val, "recorder_restored")
                        except (ValueError, TypeError):
                            continue
            except Exception as ex:
                _LOGGER.debug(f"Could not query recorder for solar forecast: {ex}")

        except Exception:
            pass
        return None

    def _can_make_api_call(self) -> bool:
        """Check if we can make another API call without exceeding the daily limit."""
        # Solcast resets at UTC midnight, so use UTC date
        today_str = dt_util.utcnow().strftime("%Y-%m-%d")
        if self._api_calls_date != today_str:
            # New day — would be reset in _track_api_call
            return True
        return self._api_calls_today < self.DAILY_API_LIMIT

    def _track_api_call(self) -> None:
        """Track API call for rate limit awareness."""
        # Solcast resets at UTC midnight, so use UTC date
        today_str = dt_util.utcnow().strftime("%Y-%m-%d")
        if self._api_calls_date != today_str:
            # New UTC day - reset counter
            self._api_calls_date = today_str
            self._api_calls_today = 0
            self._rate_limited = False

        self._api_calls_today += 1

        if self._api_calls_today >= self.DAILY_API_LIMIT:
            self._rate_limited = True
            _LOGGER.warning(
                f"Solcast API daily limit reached ({self._api_calls_today}/{self.DAILY_API_LIMIT}). "
                f"Using cached data until tomorrow."
            )

        # Persist to survive restarts
        self.hass.async_create_task(
            self._rate_limit_store.async_save({
                "date": today_str,
                "calls": self._api_calls_today,
            })
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch forecast data from Solcast.

        First checks if the Solcast HA integration is installed - if so, reads from
        its sensors to avoid doubling API calls. Only makes direct API calls if the
        Solcast integration is not available.

        Supports multiple resource IDs - values are combined by summing.

        IMPORTANT: We skip estimated_actuals API calls to conserve API budget.
        The hobbyist tier only allows 10 calls/day, and with split arrays each
        resource requires its own call. Estimated actuals are optional - we use
        cached full-day forecasts instead.
        """
        # Restore rate limit state on first run (persisted across restarts)
        if self._api_calls_date is None:
            await self._restore_rate_limit_state()
            # Restore the full-day forecast cache too. Without this, restarting
            # mid-day leaves _daily_forecast_date == None and the fetch logic
            # below falls into the "new day → cache today_remaining" fallback
            # that makes the sensor display only the rest-of-day forecast.
            await self._restore_daily_forecast_cache()

        # First, check if Solcast HA integration is installed and has data
        # This avoids doubling API calls if user has both integrations
        solcast_data = await self._try_read_from_solcast_integration()
        if solcast_data:
            # Guard: if the integration reports 0 but we have cached non-zero data,
            # the integration is likely rate-limited — use cached data instead.
            # Today's total forecast should never drop to 0 mid-day.
            new_kwh = solcast_data.get("today_forecast_kwh", 0)
            cached_kwh = self.data.get("today_forecast_kwh", 0) if self.data else 0
            if new_kwh == 0 and cached_kwh > 0:
                _LOGGER.info(
                    f"Solcast HA integration reported 0kWh but cached forecast is "
                    f"{cached_kwh:.1f}kWh — likely rate-limited, using cached data"
                )
                return self.data
            _LOGGER.debug("Using data from Solcast HA integration (no API calls needed)")
            # Persist good data so it survives restarts
            self.hass.async_create_task(self._save_forecast_cache(solcast_data))
            return solcast_data

        # Check if we're rate limited — but verify with a real API call
        # on first update after restore (persisted counter may be stale)
        if self._rate_limited:
            if self.data and self.data.get("today_forecast_kwh", 0) > 0:
                _LOGGER.debug(
                    f"Solcast API rate limited - using cached forecast data. "
                    f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                )
                return self.data
            # No in-memory data — counter may be stale from a previous timezone
            # mismatch or old persisted state. Try one verification call.
            if not getattr(self, "_rate_limit_verified", False):
                self._rate_limit_verified = True
                _LOGGER.info(
                    "Solcast rate-limited from restore — verifying with one API call"
                )
                # Temporarily clear rate limit so the fetch logic runs
                self._rate_limited = False
                self._api_calls_today = 0
                # Fall through to the fetch logic below
            else:
                # Already verified, genuinely rate limited
                restored = await self._restore_forecast_cache()
                if restored:
                    _LOGGER.info(
                        f"Solcast API rate limited - restored forecast from storage. "
                        f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                    )
                    return restored
                restored = await self._restore_from_ha_state()
                if restored:
                    _LOGGER.info(
                        f"Solcast API rate limited - restored forecast from HA sensor state. "
                        f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                    )
                    return restored
                _LOGGER.warning(
                    f"Solcast API rate limited and no cached forecast available. "
                    f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                )
                return self.data or {"available": False}

        # Solcast integration not available - make our own API calls
        # Hard guard: refuse to make API calls if daily limit already reached
        n_resources = len(self._resource_ids)
        if self._api_calls_today + n_resources > self.DAILY_API_LIMIT:
            _LOGGER.warning(
                f"Solcast API: skipping fetch — would exceed daily limit "
                f"({self._api_calls_today} + {n_resources} > {self.DAILY_API_LIMIT}). "
                f"Using cached data."
            )
            self._rate_limited = True
            if self.data and self.data.get("today_forecast_kwh", 0) > 0:
                return self.data
            restored = await self._restore_forecast_cache()
            if restored:
                return restored
            restored = await self._restore_from_ha_state()
            if restored:
                return restored
            return self.data or {"available": False}

        try:
            async with asyncio.timeout(60):  # Longer timeout for multiple API calls
                _LOGGER.info(
                    f"Fetching Solcast forecast for {n_resources} resource(s). "
                    f"API calls today: {self._api_calls_today}/{self.DAILY_API_LIMIT}"
                )

                # Fetch forecasts from first resource
                self._track_api_call()
                forecasts = await self._fetch_forecast_for_resource(self._resource_ids[0])
                if not forecasts:
                    _LOGGER.warning("No forecasts from Solcast API")
                    if self.data and self.data.get("today_forecast_kwh", 0) > 0:
                        return self.data
                    # Try persistent cache (survives restarts)
                    restored = await self._restore_forecast_cache()
                    if restored:
                        _LOGGER.info("Restored solar forecast from persistent cache after API failure")
                        return restored
                    # Last resort: read last known sensor state from HA
                    restored = await self._restore_from_ha_state()
                    if restored:
                        _LOGGER.info("Restored solar forecast from HA sensor state after API failure")
                        return restored
                    return {"available": False}

                # NOTE: We intentionally skip estimated_actuals to save API calls
                # With 10 calls/day limit and split arrays, we need to conserve budget
                # The full-day forecast will be estimated from cached values instead
                estimated_actuals = None

                # If multiple resources, fetch and combine
                if len(self._resource_ids) > 1:
                    for resource_id in self._resource_ids[1:]:
                        if not self._can_make_api_call():
                            _LOGGER.warning(
                                f"Solcast API daily limit reached — skipping resource {resource_id[:8]}..."
                            )
                            break
                        self._track_api_call()
                        additional_forecasts = await self._fetch_forecast_for_resource(resource_id)
                        if additional_forecasts:
                            forecasts = self._combine_forecasts(forecasts, additional_forecasts)
                        else:
                            _LOGGER.warning(f"Failed to fetch forecast from resource {resource_id[:8]}...")

                    _LOGGER.info(f"Combined data from {len(self._resource_ids)} Solcast sites")

            if not forecasts:
                return {"available": False}

            # Calculate totals
            now = dt_util.now()
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
            tomorrow_end = today_end + timedelta(days=1)

            today_past = 0.0  # Production that already happened today (from estimated_actuals)
            today_remaining = 0.0  # Future production today (from forecasts)
            tomorrow_total = 0.0
            today_peak = 0.0
            tomorrow_peak = 0.0
            current_estimate = None
            period_hours = 0.5  # 30-minute periods

            # Sum up past production from estimated_actuals (today only)
            if estimated_actuals:
                for actual in estimated_actuals:
                    period_end_str = actual.get("period_end", "")
                    pv_estimate = self._get_pv_estimate(actual)

                    try:
                        period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                        period_end_local = dt_util.as_local(period_end)

                        # Only count today's past production
                        if today_start <= period_end_local <= now:
                            today_past += pv_estimate * period_hours
                            today_peak = max(today_peak, pv_estimate)
                    except (ValueError, TypeError):
                        pass

            # Sum up future production from forecasts
            for forecast in forecasts:
                period_end_str = forecast.get("period_end", "")
                pv_estimate = self._get_pv_estimate(forecast)

                try:
                    period_end = datetime.fromisoformat(period_end_str.replace("Z", "+00:00"))
                    period_end_local = dt_util.as_local(period_end)

                    # Set current estimate to first forecast period
                    if current_estimate is None:
                        current_estimate = pv_estimate

                    if period_end_local <= today_end:
                        today_remaining += pv_estimate * period_hours
                        today_peak = max(today_peak, pv_estimate)
                    elif period_end_local <= tomorrow_end:
                        tomorrow_total += pv_estimate * period_hours
                        tomorrow_peak = max(tomorrow_peak, pv_estimate)

                except (ValueError, TypeError) as e:
                    _LOGGER.debug(f"Error parsing forecast period: {e}")

            # Full day calculation
            today_str = now.strftime("%Y-%m-%d")

            if today_past > 0:
                # We have estimated actuals - use actual + remaining
                today_forecast = today_past + today_remaining
                # Update cache with this more accurate value
                self._daily_forecast_date = today_str
                self._daily_forecast_kwh = today_forecast
                self._daily_forecast_peak_kw = today_peak
                _LOGGER.info(
                    f"Solcast forecast updated: Today total={today_forecast:.1f}kWh "
                    f"(past={today_past:.1f}kWh + remaining={today_remaining:.1f}kWh), "
                    f"peak={today_peak:.2f}kW, Tomorrow={tomorrow_total:.1f}kWh"
                )
            else:
                # No estimated actuals - use cached full-day or remaining as fallback
                if self._daily_forecast_date != today_str:
                    # Cached date doesn't match today — either a genuine new day
                    # (midnight rollover) or a restart where _restore_daily_forecast_cache
                    # couldn't find a valid cache. In the genuine new-day case
                    # `now` is early morning and `today_remaining` ≈ today_total,
                    # so caching it is fine. In the restart-mid-day case the
                    # value will be suspiciously low — log a hint so users can
                    # tell the two apart.
                    is_likely_partial_day = now.hour >= 10 and today_remaining < 5.0
                    if is_likely_partial_day:
                        _LOGGER.warning(
                            "Solcast: caching partial-day remaining (%.1fkWh) as today's "
                            "forecast because no full-day cache was restored. "
                            "If this is a restart after %02d:00, the forecast will be "
                            "under-reported until the next UTC day rollover.",
                            today_remaining, now.hour,
                        )
                    self._daily_forecast_date = today_str
                    self._daily_forecast_kwh = today_remaining
                    self._daily_forecast_peak_kw = today_peak
                    today_forecast = today_remaining
                    _LOGGER.info(
                        f"Solcast: New day, cached forecast for {today_str}: {today_remaining:.1f}kWh"
                    )
                else:
                    # Use cached value (from earlier fetch today or restored
                    # full-day cache from persistent storage). Never downgrade
                    # the cached full-day total to the current remaining — it's
                    # always an under-estimate after mid-morning.
                    today_forecast = self._daily_forecast_kwh or today_remaining
                    today_peak = self._daily_forecast_peak_kw or today_peak
                    _LOGGER.info(
                        f"Solcast forecast updated: Today={today_forecast:.1f}kWh (cached), "
                        f"remaining={today_remaining:.1f}kWh, Tomorrow={tomorrow_total:.1f}kWh"
                    )

            result = {
                "available": True,
                "today_forecast_kwh": round(today_forecast, 2),  # Full day (actuals + forecast)
                "today_remaining_kwh": round(today_remaining, 2),  # Remaining from now
                "today_total_kwh": round(today_forecast, 2),  # Alias for backward compat
                "tomorrow_total_kwh": round(tomorrow_total, 2),
                "today_peak_kw": round(today_peak, 2),
                "tomorrow_peak_kw": round(tomorrow_peak, 2),
                "current_estimate_kw": round(current_estimate, 2) if current_estimate else None,
                "forecast_periods": len(forecasts),
                "forecasts": forecasts,  # Raw forecast periods for optimizer
                "estimate_type": self._estimate_type,
                "last_update": dt_util.utcnow(),
                "source": "api",
            }
            # Persist good forecast data so it survives restarts
            self.hass.async_create_task(self._save_forecast_cache(result))
            return result

        except asyncio.TimeoutError as err:
            raise UpdateFailed("Timeout fetching Solcast forecast") from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"Error fetching Solcast forecast: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error fetching Solcast forecast: {err}") from err


class OctopusPriceCoordinator(DataUpdateCoordinator):
    """Coordinator to fetch Octopus Energy UK price data.

    Fetches half-hourly import and export rates from the Octopus Energy API.
    Converts to Amber-compatible format for use with existing tariff conversion.

    Key differences from Amber:
    - Prices in pence/kWh (not cents)
    - Prices include VAT (5%)
    - 30-minute intervals
    - Prices published daily after 4pm UK time for next day
    - Can go negative (you get paid to use electricity)
    - Price cap at 100p/kWh
    """

    def __init__(
        self,
        hass: HomeAssistant,
        product_code: str,
        tariff_code: str,
        gsp_region: str,
        export_product_code: str | None = None,
        export_tariff_code: str | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            product_code: Octopus product code (e.g., "AGILE-24-10-01")
            tariff_code: Full tariff code including region (e.g., "E-1R-AGILE-24-10-01-A")
            gsp_region: UK Grid Supply Point region code (e.g., "A")
            export_product_code: Optional export product code for Agile Outgoing/Flux
            export_tariff_code: Optional export tariff code
        """
        from .octopus_api import OctopusAPIClient

        self.product_code = product_code
        self.tariff_code = tariff_code
        self.gsp_region = gsp_region
        self.export_product_code = export_product_code
        self.export_tariff_code = export_tariff_code
        self.session = async_get_clientsession(hass)
        self._client = OctopusAPIClient(self.session)

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_octopus_prices",
            update_interval=timedelta(minutes=30),  # Octopus updates less frequently than Amber
        )

    @staticmethod
    def _expand_to_half_hourly(rates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Expand block rates into individual 30-minute entries.

        Agile rates (already 30-min) pass through unchanged. Go rates (2 blocks/day)
        and Tracker rates (1 block/day) are split into 30-min chunks so the LP
        optimizer sees 48 price points instead of 1-2.

        Args:
            rates: List of rate dicts with valid_from, valid_to, and price fields

        Returns:
            List of rate dicts, each covering exactly 30 minutes
        """
        expanded: list[dict[str, Any]] = []

        for rate in rates:
            valid_from_str = rate.get("valid_from", "")
            valid_to_str = rate.get("valid_to", "")

            if not valid_from_str or not valid_to_str:
                expanded.append(rate)
                continue

            try:
                vf = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                vt = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                expanded.append(rate)
                continue

            duration = vt - vf
            if duration <= timedelta(minutes=30):
                # Already 30-min or shorter — pass through
                expanded.append(rate)
                continue

            # Split into 30-min chunks
            chunk_start = vf
            while chunk_start < vt:
                chunk_end = min(chunk_start + timedelta(minutes=30), vt)
                chunk = dict(rate)
                chunk["valid_from"] = chunk_start.isoformat()
                chunk["valid_to"] = chunk_end.isoformat()
                expanded.append(chunk)
                chunk_start = chunk_end

        return expanded

    def _read_from_octopus_energy_integration(self) -> dict[str, Any] | None:
        """Try to read rates from the BottlecapDave/HomeAssistant-OctopusEnergy integration.

        When the octopus_energy integration is installed, read import and export rates
        directly from its coordinators instead of making our own API calls.

        Returns Amber-compatible format dict, or None if integration not available.
        """
        from datetime import timezone

        oe_data = self.hass.data.get("octopus_energy")
        if not oe_data or not isinstance(oe_data, dict):
            return self._read_from_octopus_energy_entities()

        now = datetime.now(timezone.utc)
        import_rates_raw: list[dict] = []
        export_rates_raw: list[dict] = []
        import_tariff = None
        export_tariff = None

        for account_id, account_data in oe_data.items():
            if not isinstance(account_data, dict):
                continue

            # Get account info to find meter points
            account_result = account_data.get("ACCOUNT")
            if not account_result:
                continue

            account_info = getattr(account_result, "account", None)
            if not account_info or not isinstance(account_info, dict):
                continue

            # Iterate electricity meter points
            meter_points = account_info.get("electricity_meter_points", [])
            for mp in meter_points:
                if not isinstance(mp, dict):
                    continue

                mpan = mp.get("mpan", "")
                meters = mp.get("meters", [])
                if not meters:
                    continue

                serial = meters[0].get("serial_number", "") if isinstance(meters[0], dict) else ""
                is_export = meters[0].get("is_export", False) if isinstance(meters[0], dict) else False

                # Get rates from coordinator
                rates_key = f"ELECTRICITY_RATES_{mpan}_{serial}"
                rates_result = account_data.get(rates_key)
                if not rates_result:
                    continue

                rates = getattr(rates_result, "rates", None) or getattr(rates_result, "original_rates", None)
                if not rates or not isinstance(rates, list):
                    continue

                # Get tariff code from active agreement
                agreements = mp.get("agreements", [])
                tariff_code = None
                for agreement in agreements:
                    if isinstance(agreement, dict):
                        tariff_code = agreement.get("tariff_code")
                        if tariff_code:
                            break

                if is_export:
                    export_rates_raw = rates
                    export_tariff = tariff_code
                else:
                    import_rates_raw = rates
                    import_tariff = tariff_code

        if not import_rates_raw:
            return self._read_from_octopus_energy_entities()

        # Promote BottlecapDave's active tariff/product code so callers (e.g.
        # the LP optimizer's AGILE/FLUX dynamic-pricing gate) see the live
        # tariff rather than whatever was set in the config flow.
        if import_tariff:
            self.tariff_code = import_tariff
            # Tariff code format: E-1R-AGILE-24-10-01-A (region letter trailing).
            # Derive product_code by stripping the leading E-{1R|2R}- prefix and
            # the trailing -A region letter, keeping the middle segment.
            try:
                parts = import_tariff.split("-")
                if len(parts) >= 5 and parts[0] == "E":
                    self.product_code = "-".join(parts[2:-1])
            except Exception:
                pass

        # Convert octopus_energy rate format to our Amber-compatible format
        current_prices: list[dict] = []
        forecast_prices: list[dict] = []
        export_forecast: list[dict] = []

        for rate in import_rates_raw:
            start = rate.get("start") or rate.get("valid_from")
            end = rate.get("end") or rate.get("valid_to")
            price_pence = rate.get("value_inc_vat", 0)

            if not start or not end:
                continue

            # Normalize to datetime objects
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))

            # Duration in minutes — BottlecapDave usually emits 30-min slots,
            # but block tariffs (Go/Cosy off-peak windows) can come through
            # as wider intervals. Compute from timestamps so downstream LP
            # expansion sees the correct slot count.
            duration_min = max(1, int((end - start).total_seconds() // 60))

            if start <= now < end:
                interval_type = "CurrentInterval"
            elif end <= now:
                interval_type = "ActualInterval"
            else:
                interval_type = "ForecastInterval"

            amber_entry = {
                "nemTime": end.isoformat(),
                "perKwh": price_pence,  # pence/kWh maps to cents
                "channelType": "general",
                "type": interval_type,
                "duration": duration_min,
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
            }

            if interval_type == "CurrentInterval":
                current_prices.append(amber_entry)
            forecast_prices.append(amber_entry)

        for rate in export_rates_raw:
            start = rate.get("start") or rate.get("valid_from")
            end = rate.get("end") or rate.get("valid_to")
            price_pence = rate.get("value_inc_vat", 0)

            if not start or not end:
                continue

            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))

            duration_min = max(1, int((end - start).total_seconds() // 60))

            if start <= now < end:
                interval_type = "CurrentInterval"
            elif end <= now:
                interval_type = "ActualInterval"
            else:
                interval_type = "ForecastInterval"

            amber_entry = {
                "nemTime": end.isoformat(),
                "perKwh": -price_pence,  # Negative = you get paid (Amber convention)
                "channelType": "feedIn",
                "type": interval_type,
                "duration": duration_min,
                "valid_from": start.isoformat(),
                "valid_to": end.isoformat(),
            }

            if interval_type == "CurrentInterval":
                current_prices.append(amber_entry)
            export_forecast.append(amber_entry)

        if not export_forecast:
            default_export_pence = 4.1
            for price in forecast_prices:
                amber_entry = dict(price)
                amber_entry["perKwh"] = -default_export_pence
                amber_entry["channelType"] = "feedIn"

                if amber_entry.get("type") == "CurrentInterval":
                    current_prices.append(amber_entry)
                export_forecast.append(amber_entry)
            export_tariff = export_tariff or "synthetic_seg"

        combined_forecast = forecast_prices + export_forecast

        current_import = next(
            (p["perKwh"] for p in current_prices if p["channelType"] == "general"),
            None,
        )
        current_export = next(
            (p["perKwh"] for p in current_prices if p["channelType"] == "feedIn"),
            None,
        )

        _LOGGER.info(
            "🐙 Using octopus_energy integration data: "
            "current_import=%.2fp/kWh, current_export=%.2fp/kWh, "
            "periods=%d (import=%d, export=%d), "
            "import_tariff=%s, export_tariff=%s",
            current_import or 0,
            -(current_export or 0),
            len(combined_forecast),
            len(forecast_prices),
            len(export_forecast),
            import_tariff or "unknown",
            export_tariff or "none",
        )

        if not current_prices:
            entity_data = self._read_from_octopus_energy_entities()
            if entity_data:
                return entity_data

        return {
            "current": current_prices,
            "forecast": combined_forecast,
            "export_rates": export_forecast,
            "last_update": dt_util.utcnow(),
            "source": "octopus_energy_integration",
            "product_code": self.product_code,
            "tariff_code": import_tariff or self.tariff_code,
            "gsp_region": self.gsp_region,
        }

    @staticmethod
    def _parse_octopus_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            dt_value = value
        elif isinstance(value, str) and value:
            try:
                dt_value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        if dt_value.tzinfo is None:
            from datetime import timezone
            dt_value = dt_value.replace(tzinfo=timezone.utc)
        return dt_value

    @staticmethod
    def _octopus_rate_to_pence(value: Any) -> float | None:
        """Normalize BottlecapDave public entity GBP rates or internal pence rates."""
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return None
        # Public current_rate entities are GBP/kWh (e.g. 0.245), while internal
        # coordinator/API rates are p/kWh (e.g. 24.5).
        return round(rate * 100 if abs(rate) <= 2 else rate, 6)

    def _octopus_state_entries(self, domain: str) -> list[Any]:
        states = getattr(self.hass, "states", None)
        if states is None:
            return []
        if hasattr(states, "async_all"):
            return list(states.async_all(domain))
        if isinstance(states, dict):
            return [
                state for entity_id, state in states.items()
                if str(entity_id).split(".", 1)[0] == domain
            ]
        return []

    def _build_octopus_amber_entry(
        self,
        start: Any,
        end: Any,
        rate_value: Any,
        channel: str,
        now: datetime,
    ) -> dict[str, Any] | None:
        start_dt = self._parse_octopus_datetime(start)
        end_dt = self._parse_octopus_datetime(end)
        rate_pence = self._octopus_rate_to_pence(rate_value)
        if start_dt is None or end_dt is None or rate_pence is None:
            return None

        if start_dt <= now < end_dt:
            interval_type = "CurrentInterval"
        elif end_dt <= now:
            interval_type = "ActualInterval"
        else:
            interval_type = "ForecastInterval"

        return {
            "nemTime": end_dt.isoformat(),
            "perKwh": -rate_pence if channel == "feedIn" else rate_pence,
            "channelType": channel,
            "type": interval_type,
            "duration": max(1, int((end_dt - start_dt).total_seconds() // 60)),
            "valid_from": start_dt.isoformat(),
            "valid_to": end_dt.isoformat(),
        }

    def _read_from_octopus_energy_entities(self) -> dict[str, Any] | None:
        """Read BottlecapDave's documented public entities as a compatibility fallback."""
        from datetime import timezone

        now = datetime.now(timezone.utc)
        current_prices: list[dict[str, Any]] = []
        import_forecast: list[dict[str, Any]] = []
        export_forecast: list[dict[str, Any]] = []
        import_tariff = None
        export_tariff = None

        for state in self._octopus_state_entries("sensor"):
            entity_id = getattr(state, "entity_id", "")
            if (
                not entity_id.startswith("sensor.octopus_energy_electricity_")
                or not entity_id.endswith("_current_rate")
            ):
                continue
            if getattr(state, "state", None) in (None, "unknown", "unavailable", ""):
                continue

            attrs = getattr(state, "attributes", None) or {}
            is_export = bool(attrs.get("is_export")) or "_export_" in entity_id
            channel = "feedIn" if is_export else "general"
            entry = self._build_octopus_amber_entry(
                attrs.get("start"),
                attrs.get("end"),
                getattr(state, "state", None),
                channel,
                now,
            )
            if not entry:
                continue
            if entry["type"] == "CurrentInterval":
                current_prices.append(entry)
            if channel == "feedIn":
                export_forecast.append(entry)
                export_tariff = attrs.get("tariff") or export_tariff
            else:
                import_forecast.append(entry)
                import_tariff = attrs.get("tariff") or import_tariff

        for state in self._octopus_state_entries("event"):
            entity_id = getattr(state, "entity_id", "")
            if (
                not entity_id.startswith("event.octopus_energy_electricity_")
                or not (
                    entity_id.endswith("_current_day_rates")
                    or entity_id.endswith("_next_day_rates")
                )
            ):
                continue

            attrs = getattr(state, "attributes", None) or {}
            rates = attrs.get("rates")
            if not isinstance(rates, list):
                continue
            is_export = bool(attrs.get("is_export")) or "_export_" in entity_id
            channel = "feedIn" if is_export else "general"
            for rate in rates:
                if not isinstance(rate, dict):
                    continue
                entry = self._build_octopus_amber_entry(
                    rate.get("start"),
                    rate.get("end"),
                    rate.get("value_inc_vat"),
                    channel,
                    now,
                )
                if not entry:
                    continue
                if entry["type"] == "CurrentInterval":
                    current_prices.append(entry)
                if channel == "feedIn":
                    export_forecast.append(entry)
                    export_tariff = attrs.get("tariff_code") or export_tariff
                else:
                    import_forecast.append(entry)
                    import_tariff = attrs.get("tariff_code") or import_tariff

        if not import_forecast and not any(
            price.get("channelType") == "general" for price in current_prices
        ):
            return None

        if not import_forecast:
            import_forecast = [
                price for price in current_prices
                if price.get("channelType") == "general"
            ]
        if not export_forecast:
            for price in import_forecast:
                entry = dict(price)
                entry["perKwh"] = -4.1
                entry["channelType"] = "feedIn"
                if entry.get("type") == "CurrentInterval":
                    current_prices.append(entry)
                export_forecast.append(entry)
            export_tariff = export_tariff or "synthetic_seg"

        if not any(price.get("channelType") == "feedIn" for price in current_prices):
            current_export = next(
                (price for price in export_forecast if price.get("type") == "CurrentInterval"),
                None,
            )
            if current_export:
                current_prices.append(current_export)

        combined_forecast = import_forecast + export_forecast
        if not current_prices:
            return None

        _LOGGER.info(
            "🐙 Using octopus_energy public entity data: periods=%d (import=%d, export=%d), "
            "import_tariff=%s, export_tariff=%s",
            len(combined_forecast),
            len(import_forecast),
            len(export_forecast),
            import_tariff or "unknown",
            export_tariff or "none",
        )

        return {
            "current": current_prices,
            "forecast": combined_forecast,
            "export_rates": export_forecast,
            "last_update": dt_util.utcnow(),
            "source": "octopus_energy_entities",
            "product_code": self.product_code,
            "tariff_code": import_tariff or self.tariff_code,
            "gsp_region": self.gsp_region,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Octopus Energy integration or API, in Amber-compatible format.

        Prefers the octopus_energy integration (BottlecapDave) when installed
        to avoid double API calls and get the correct export tariff automatically.

        Returns:
            dict with 'current', 'forecast', 'export_rates', and 'last_update'
            in Amber-compatible format for use with tariff conversion.
        """
        try:
            # Try reading from octopus_energy integration first
            integration_data = self._read_from_octopus_energy_integration()
            if integration_data:
                return integration_data

            from datetime import timezone

            now = datetime.now(timezone.utc)

            # Fetch import rates for next 48 hours
            period_from = now - timedelta(hours=1)  # Include recent past
            period_to = now + timedelta(hours=48)

            import_rates = await self._client.get_current_rates(
                self.product_code,
                self.tariff_code,
                period_from=period_from,
                period_to=period_to,
                page_size=200,  # 48 hours = 96 periods, add buffer
            )

            # Expand block rates (Go/Tracker) into half-hourly entries
            import_rates = self._expand_to_half_hourly(import_rates)

            if not import_rates:
                raise UpdateFailed(
                    f"No import rates returned from Octopus API for {self.tariff_code}"
                )

            # Fetch export rates if configured
            export_rates = []
            if self.export_product_code and self.export_tariff_code:
                export_rates = await self._client.get_export_rates(
                    self.export_product_code,
                    self.export_tariff_code,
                    period_from=period_from,
                    period_to=period_to,
                    page_size=200,
                )
                export_rates = self._expand_to_half_hourly(export_rates)

            # Convert to Amber-compatible format
            current_prices = []
            forecast_prices = []

            for rate in import_rates:
                valid_from_str = rate.get("valid_from", "")
                valid_to_str = rate.get("valid_to", "")
                price_pence = rate.get("value_inc_vat", 0)

                if not valid_from_str or not valid_to_str:
                    continue

                # Parse timestamps
                try:
                    valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                    valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                # Determine interval type based on timing
                # Octopus uses valid_to as the interval end time (same convention as Amber's nemTime)
                if valid_from <= now < valid_to:
                    interval_type = "CurrentInterval"
                elif valid_to <= now:
                    interval_type = "ActualInterval"
                else:
                    interval_type = "ForecastInterval"

                # Build Amber-compatible price entry
                # Note: price_pence is in pence/kWh, which maps directly to cents for Tesla
                # (Tesla doesn't care about currency, just the numeric value)
                amber_entry = {
                    "nemTime": valid_to.isoformat(),  # Amber uses interval END time
                    "perKwh": price_pence,  # pence/kWh (treated as cents)
                    "channelType": "general",
                    "type": interval_type,
                    "duration": 30,  # 30-minute intervals
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                }

                if interval_type == "CurrentInterval":
                    current_prices.append(amber_entry)
                forecast_prices.append(amber_entry)

            # Process export rates if available
            export_forecast = []
            for rate in export_rates:
                valid_from_str = rate.get("valid_from", "")
                valid_to_str = rate.get("valid_to", "")
                price_pence = rate.get("value_inc_vat", 0)

                if not valid_from_str or not valid_to_str:
                    continue

                try:
                    valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                    valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                except ValueError:
                    continue

                if valid_from <= now < valid_to:
                    interval_type = "CurrentInterval"
                elif valid_to <= now:
                    interval_type = "ActualInterval"
                else:
                    interval_type = "ForecastInterval"

                # Export prices: Amber uses negative for "you get paid"
                # Octopus export rates are positive (payment to you)
                # Convert to Amber convention: negative = payment to you
                amber_entry = {
                    "nemTime": valid_to.isoformat(),
                    "perKwh": -price_pence,  # Negative = you get paid
                    "channelType": "feedIn",
                    "type": interval_type,
                    "duration": 30,
                    "valid_from": valid_from.isoformat(),
                    "valid_to": valid_to.isoformat(),
                }

                if interval_type == "CurrentInterval":
                    current_prices.append(amber_entry)
                export_forecast.append(amber_entry)

            # If no export rates configured, create synthetic export prices
            # (typically 0 for non-export tariffs, or use SEG rates)
            if not export_rates:
                for rate in import_rates:
                    valid_from_str = rate.get("valid_from", "")
                    valid_to_str = rate.get("valid_to", "")

                    if not valid_from_str or not valid_to_str:
                        continue

                    try:
                        valid_from = datetime.fromisoformat(valid_from_str.replace("Z", "+00:00"))
                        valid_to = datetime.fromisoformat(valid_to_str.replace("Z", "+00:00"))
                    except ValueError:
                        continue

                    if valid_from <= now < valid_to:
                        interval_type = "CurrentInterval"
                    elif valid_to <= now:
                        interval_type = "ActualInterval"
                    else:
                        interval_type = "ForecastInterval"

                    # Default export rate: Smart Export Guarantee minimum (typically 4.1p)
                    # or 0 if tariff doesn't support export
                    default_export_pence = 4.1  # SEG minimum

                    amber_entry = {
                        "nemTime": valid_to.isoformat(),
                        "perKwh": -default_export_pence,  # Negative = you get paid
                        "channelType": "feedIn",
                        "type": interval_type,
                        "duration": 30,
                        "valid_from": valid_from.isoformat(),
                        "valid_to": valid_to.isoformat(),
                    }

                    if interval_type == "CurrentInterval":
                        current_prices.append(amber_entry)
                    export_forecast.append(amber_entry)

            # Combine import and export forecasts
            combined_forecast = forecast_prices + export_forecast

            # Log summary
            current_import = next(
                (p["perKwh"] for p in current_prices if p["channelType"] == "general"),
                None,
            )
            current_export = next(
                (p["perKwh"] for p in current_prices if p["channelType"] == "feedIn"),
                None,
            )

            _LOGGER.info(
                "Octopus API data for %s: current_import=%.2fp/kWh, current_export=%.2fp/kWh, "
                "forecast_periods=%d (import=%d, export=%d)",
                self.tariff_code,
                current_import or 0,
                -(current_export or 0),  # Un-negate for display
                len(combined_forecast),
                len(forecast_prices),
                len(export_forecast),
            )

            return {
                "current": current_prices,
                "forecast": combined_forecast,
                "export_rates": export_forecast,
                "last_update": dt_util.utcnow(),
                "source": "octopus_api",
                "product_code": self.product_code,
                "tariff_code": self.tariff_code,
                "gsp_region": self.gsp_region,
            }

        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching Octopus data: {err}") from err


class OctopusSavingSessionCoordinator(DataUpdateCoordinator):
    """Coordinator that polls for Octopus Saving Sessions.

    Supports two data sources:
    - Direct API: Uses OctopusSavingSessionsClient with GraphQL
    - Entity: Reads from Bottlecap Dave's Octopus integration event entity

    Polls every 15 minutes. Optionally auto-joins available sessions.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        client=None,
        entity_id: str | None = None,
        auto_join: bool = False,
        octopoints_per_penny: int = 8,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: HomeAssistant instance
            client: OctopusSavingSessionsClient (direct mode) or None
            entity_id: Bottlecap Dave event entity ID (entity mode) or None
            auto_join: Auto-join available sessions (direct API or Dave's integration)
            octopoints_per_penny: Conversion rate (default 8)
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_octopus_saving_sessions",
            update_interval=timedelta(minutes=15),
        )
        self._client = client
        self._entity_id = entity_id
        self._auto_join = auto_join
        self._octopoints_per_penny = octopoints_per_penny

    async def _async_update_data(self) -> dict:
        """Fetch sessions from direct API or Bottlecap Dave entity."""
        from .octopus_sessions import (
            SavingSession,
            saving_session_from_octopus_energy_event,
        )

        sessions: list[SavingSession] = []

        if self._client:
            # Direct API mode
            try:
                raw = await self._client.get_sessions()
                if self._auto_join:
                    for s in raw:
                        if not s.joined and s.session_type == "saving":
                            joined = await self._client.join_session(s.code)
                            if joined:
                                s.joined = True
                                _LOGGER.info(
                                    "Auto-joined saving session: %s (%s - %s)",
                                    s.code, s.start, s.end,
                                )
                sessions = raw
            except Exception as err:
                _LOGGER.error("Error fetching saving sessions from API: %s", err)

        elif self._entity_id:
            # Bottlecap Dave entity mode — reads from octopus_energy event entity
            state = self.hass.states.get(self._entity_id)
            if state:
                sessions_by_key: dict[tuple[datetime, datetime], SavingSession] = {}

                def add_session(session: SavingSession | None) -> None:
                    if session is None:
                        return
                    sessions_by_key[(session.start, session.end)] = session

                # Auto-join available sessions via Dave's service
                if self._auto_join:
                    available = state.attributes.get("available_events", [])
                    for ev in available:
                        try:
                            code = ev.get("code", "")
                            if not code:
                                continue
                            _LOGGER.info(
                                "🐙 Auto-joining saving session via octopus_energy: %s "
                                "(octopoints=%s/kWh)",
                                code, ev.get("octopoints_per_kwh", "?"),
                            )
                            await self.hass.services.async_call(
                                "octopus_energy",
                                "join_octoplus_saving_session_event",
                                {"event_code": code},
                                target={"entity_id": self._entity_id},
                                blocking=True,
                            )
                            _LOGGER.info(
                                "✅ Joined saving session %s via octopus_energy", code,
                            )
                            # Dave's integration schedules a refresh after joining, so
                            # expose the successfully joined event immediately for the
                            # next optimiser run instead of waiting for a later poll.
                            add_session(
                                saving_session_from_octopus_energy_event(
                                    ev,
                                    joined=True,
                                )
                            )
                        except Exception as err:
                            _LOGGER.error(
                                "Failed to auto-join saving session %s: %s", code, err,
                            )

                # Parse joined_events from entity attributes
                for ev in state.attributes.get("joined_events", []):
                    session = saving_session_from_octopus_energy_event(
                        ev,
                        joined=True,
                    )
                    if session is None:
                        _LOGGER.debug("Skipping malformed entity event: %s", ev)
                        continue
                    add_session(session)

                sessions = sorted(sessions_by_key.values(), key=lambda s: s.start)
            else:
                _LOGGER.debug(
                    "Saving sessions entity %s not available", self._entity_id
                )

        sessions = sorted(sessions, key=lambda s: s.start)
        now = dt_util.utcnow()
        if getattr(now, "tzinfo", None) is None:
            now = now.replace(tzinfo=dt_util.UTC)
        else:
            now = now.astimezone(dt_util.UTC)
        return {
            "sessions": sessions,
            "active_session": next(
                (s for s in sessions if s.is_active() and s.joined), None
            ),
            "next_session": next(
                (s for s in sessions if s.start > now and s.joined), None
            ),
        }


class FlowPowerTWAPTracker:
    """Tracks wholesale prices and calculates rolling 30-day TWAP.

    The TWAP (Time Weighted Average Price) replaces the hardcoded 8.0 c/kWh
    market average in the PEA formula with an actual rolling 30-day average.

    Formula: PEA = wholesale - TWAP - 1.7 (benchmark)
    Fallback: PEA = wholesale - 8.0 - 1.7 when < 12 samples available
    """

    def __init__(self, hass: HomeAssistant, region: str, entry_id: str) -> None:
        self.hass = hass
        self.region = region
        self._price_history: list[dict] = []
        self._store = Store(hass, 1, f"power_sync.flow_power_twap.{entry_id}")
        self._last_store_save: float | None = None
        self._twap: float | None = None
        self._loaded = False

    async def async_load(self) -> None:
        """Load price history from persistent storage."""
        stored = await self._store.async_load()
        if stored and isinstance(stored.get("price_history"), list):
            self._price_history = stored["price_history"]
            self._prune_history()
            self._twap = self._calculate_twap()
            _LOGGER.info(
                "Loaded TWAP history: %d samples over %.1f days, TWAP=%.2f c/kWh%s",
                len(self._price_history),
                self.twap_days,
                self._twap if self._twap is not None else FLOW_POWER_MARKET_AVG,
                " (fallback)" if self.using_fallback else "",
            )
        self._loaded = True

    def record_price(self, wholesale_cents: float) -> None:
        """Record a wholesale price sample with 4-minute deduplication."""
        now = time.time()
        if self._price_history:
            if now - self._price_history[-1]["ts"] < 240:
                return
        self._price_history.append({"ts": round(now), "price": round(wholesale_cents, 2)})
        self._prune_history()
        self._twap = self._calculate_twap()
        # Save periodically (every 10 minutes)
        if self._last_store_save is None or now - self._last_store_save > 600:
            self.hass.async_create_task(self._async_save())
            self._last_store_save = now

    def _prune_history(self) -> None:
        """Remove entries older than the TWAP window."""
        cutoff = time.time() - (DEFAULT_TWAP_WINDOW_DAYS * 86400)
        self._price_history = [
            entry for entry in self._price_history if entry["ts"] > cutoff
        ]

    def _calculate_twap(self) -> float | None:
        """Calculate TWAP from price history. Returns None if insufficient data."""
        if len(self._price_history) < MIN_TWAP_SAMPLES:
            return None
        total = sum(entry["price"] for entry in self._price_history)
        return round(total / len(self._price_history), 2)

    async def _async_save(self) -> None:
        """Save price history to persistent storage."""
        try:
            await self._store.async_save({
                "price_history": self._price_history,
                "region": self.region,
            })
        except Exception as err:
            _LOGGER.warning("Failed to save TWAP history: %s", err)

    async def async_save(self) -> None:
        """Public save for use on unload."""
        await self._async_save()

    @property
    def twap(self) -> float | None:
        """Return the current TWAP value, or None if insufficient data."""
        return self._twap

    @property
    def twap_days(self) -> float:
        """Return how many days of price data we have."""
        if not self._price_history:
            return 0.0
        oldest = self._price_history[0]["ts"]
        return round((time.time() - oldest) / 86400, 1)

    @property
    def sample_count(self) -> int:
        """Return the number of price samples."""
        return len(self._price_history)

    @property
    def using_fallback(self) -> bool:
        """Return True if we're using the hardcoded fallback instead of dynamic TWAP."""
        return self._twap is None
