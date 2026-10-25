"""Display-only measured export bonuses and configured fixed supply costs.

This accounting never feeds an optimizer objective or a hardware command.
Only observed intervals earn a bonus. Unknown quota history withholds totals
until the next complete local-day window rather than inventing earnings.
"""
from __future__ import annotations

import calendar
import math
from datetime import datetime, timedelta, timezone
from typing import Any


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def configured_supply_charge(entry: Any, now: datetime) -> float:
    """Resolve existing major-currency daily/monthly settings."""
    settings = dict(getattr(entry, "data", {}) or {})
    settings.update(getattr(entry, "options", {}) or {})
    daily = max(0.0, _finite(settings.get("daily_supply_charge")))
    if daily > 0:
        return daily
    monthly = max(0.0, _finite(settings.get("monthly_supply_charge")))
    return monthly / calendar.monthrange(now.year, now.month)[1]


def configure_tracker(hass, entry_id, now, previous, restored,
                      legacy_mtd_export=0.0, legacy_month=None):
    """Use the same configured plan resolver as the optimizer, when available."""
    from .zerohero import zerohero_config_from_entry

    entry = hass.config_entries.async_get_entry(entry_id)
    entry_data = hass.data.get("power_sync", {}).get(entry_id, {})
    settings = dict(getattr(entry, "data", {}) or {})
    settings.update(getattr(entry, "options", {}) or {})
    config = None
    if settings.get("electricity_provider") == "globird":
        resolver = getattr(entry_data.get("optimization_coordinator"), "_zerohero_config", None)
        config = (resolver() if callable(resolver) else zerohero_config_from_entry(
            entry, entry_data.get("tariff_schedule")))
    state = dict(previous.state if previous is not None else restored)
    if (config is not None and legacy_mtd_export > 0
            and legacy_month == now.strftime("%Y-%m")
            and state.get("month") != legacy_month):
        state["unknown_prior_bonus_month"] = now.strftime("%Y-%m")
    return DailyCostTracker(config, configured_supply_charge(entry, now), state)


def _seconds(start: datetime, end: datetime) -> float:
    return (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()


def _parts(start: datetime, end: datetime, config: Any):
    """Split at local midnight and tariff edges, measuring elapsed UTC time."""
    cursor = start
    while _seconds(cursor, end) > 0:
        midnight = cursor.replace(hour=0, minute=0, second=0, microsecond=0)
        edges = [midnight + timedelta(days=1)]
        if config is not None:
            for text in (config.start, config.end):
                hour, minute = (int(part) for part in text.split(":"))
                edge = midnight + timedelta(hours=hour, minutes=minute)
                if _seconds(cursor, edge) > 0:
                    edges.append(edge)
        stop = min([end, *edges], key=lambda dt: dt.timestamp())
        minute = cursor.hour * 60 + cursor.minute
        active = False
        if config is not None:
            first, last = [sum(int(p) * m for p, m in zip(v.split(":"), (60, 1)))
                           for v in (config.start, config.end)]
            active = first <= minute < last if first < last else minute >= first or minute < last
        yield cursor.date().isoformat(), active, _seconds(cursor, stop) / 3600
        cursor = stop


def forecast_export_value_prices(config, timestamps, export_w, base_prices,
                                 interval_minutes, remaining_by_day):
    """Return quota-weighted forecast rates, sharing quota across ALL site export."""
    remaining = dict(remaining_by_day or {})
    result = []
    for index, start in enumerate(timestamps):
        base = _finite(base_prices[index])
        power = max(0.0, _finite(export_w[index])) / 1000
        end = datetime.fromtimestamp(start.timestamp() + interval_minutes * 60, start.tzinfo)
        energy = power * interval_minutes / 60
        bonus = 0.0
        for day, active, hours in _parts(start, end, config):
            if not active:
                continue
            eligible = min(power * hours, max(0.0, _finite(remaining.get(day))))
            remaining[day] = max(0.0, _finite(remaining.get(day)) - eligible)
            bonus += eligible * max(0.0, config.super_export_rate - base)
        result.append(base + bonus / energy if energy > 0 else base)
    return result


class DailyCostTracker:
    """Persist separate bonus and fixed-charge adjustments to base energy costs."""

    def __init__(self, config=None, supply_charge=0.0, state=None):
        self.config = config
        self.supply_charge = max(0.0, _finite(supply_charge))
        self.state = dict(state) if isinstance(state, dict) else {}
        self.state["supply_by_day"] = dict(self.state.get("supply_by_day") or {})

    def _begin(self, now):
        day = now.date().isoformat()
        month = day[:7]
        if self.state.get("month") != month:
            unknown_prior = self.state.get("unknown_prior_bonus_month") == month
            self.state = {"month": month, "supply_by_day": {}, "mtd_bonus": 0.0,
                          "mtd_partial": unknown_prior}
        if self.state.get("day") != day:
            self.state.update(day=day, bonus=0.0, used=0.0, partial=False)
            # If observation starts during/after the window, prior exports
            # cannot be reconstructed from a daily total.
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.config is not None and any(
                active for _day, active, _hours in _parts(midnight, now, self.config)
            ):
                self.state.update(used=self.config.export_cap_kwh, partial=True,
                                  mtd_partial=True)
        signature = (None if self.config is None else [self.config.plan,
            self.config.start, self.config.end, self.config.export_cap_kwh,
            self.config.super_export_rate])
        if "config" in self.state and self.state["config"] != signature:
            # A changed plan must not grant another same-day quota.
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if self.config is not None and any(
                active for _day, active, _hours in _parts(midnight, now, self.config)
            ):
                self.state.update(used=self.config.export_cap_kwh, partial=True,
                                  mtd_partial=True)
        self.state["config"] = signature
        self.state["supply_by_day"][day] = self.supply_charge

    def observe(self, start, end, export_kw, base_price, integrated):
        if start is None:
            saved_time = self.state.get("last_observed")
            if saved_time:
                try:
                    saved_time = datetime.fromisoformat(saved_time).astimezone(end.tzinfo)
                except (TypeError, ValueError):
                    saved_time = None
            if saved_time is not None:
                # A restart does not prove that no export happened while HA
                # was down. Retain earned value but withhold unknown totals.
                self.observe(saved_time, end, export_kw, base_price, False)
                return
            self._begin(end)
            self.state["last_observed"] = end.isoformat()
            return
        # Never replay stale readings, missing prices or unobserved downtime.
        if not integrated or base_price is None:
            self._begin(end)
            for day, active, _hours in _parts(start, end, self.config):
                if active and day[:7] == self.state["month"]:
                    self.state["mtd_partial"] = True
                    if day == self.state["day"]:
                        self.state.update(used=self.config.export_cap_kwh, partial=True)
            self.state["last_observed"] = end.isoformat()
            return
        cursor = start
        for day, active, hours in _parts(start, end, self.config):
            self._begin(cursor)
            if active:
                eligible = min(max(0.0, export_kw) * hours,
                               max(0.0, self.config.export_cap_kwh - self.state["used"]))
                bonus = eligible * max(0.0, self.config.super_export_rate - base_price)
                self.state["used"] += eligible
                self.state["bonus"] += bonus
                self.state["mtd_bonus"] += bonus
            cursor = datetime.fromtimestamp(cursor.timestamp() + hours * 3600, cursor.tzinfo)
        self._begin(end)
        self.state["last_observed"] = end.isoformat()

    def summary(self, base):
        result = dict(base)
        partial = bool(self.state.get("partial"))
        mtd_partial = bool(self.state.get("mtd_partial"))
        daily_supply = self.state.get("supply_by_day", {}).get(self.state.get("day"), 0.0)
        mtd_supply = sum(_finite(v) for v in self.state.get("supply_by_day", {}).values())
        # Supply is a calendar charge even on days without telemetry. For
        # unobserved days, use today's configured rate as an explicit estimate.
        elapsed_days = int(self.state.get("day", "0000-00-00")[-2:])
        mtd_supply += max(0, elapsed_days - len(self.state.get("supply_by_day", {}))) * self.supply_charge
        for key, bonus, missing in (
            ("export_earnings_today", self.state.get("bonus", 0.0), partial),
            ("mtd_export_earnings", self.state.get("mtd_bonus", 0.0), mtd_partial),
        ):
            result[key] = (round(result[key] + bonus, 4)
                           if result.get(key) is not None and not missing else None)
        cost, earnings = result.get("import_cost_today"), result.get("export_earnings_today")
        net = (round(cost - earnings + daily_supply, 4)
               if cost is not None and earnings is not None else None)
        result.update(daily_supply_charge=round(daily_supply, 4), net_cost_today=net,
                      export_bonus_today=round(self.state.get("bonus", 0.0), 4),
                      export_bonus_coverage="partial" if partial else "complete",
                      mtd_supply_charge=round(mtd_supply, 4),
                      supply_charge_days=elapsed_days)
        if partial:
            result["export_earnings_coverage"] = "partial"
        result["avg_cost_per_kwh_today"] = (
            round(net / result["load_today_kwh"], 4)
            if net is not None and result.get("avg_cost_per_kwh_today") is not None
            and result.get("load_today_kwh", 0) > 0 else None)
        imp, exp = result.get("mtd_import_cost"), result.get("mtd_export_earnings")
        result["avg_cost_per_kwh_mtd"] = (
            round((imp - exp + mtd_supply) / result["mtd_load_kwh"], 4)
            if imp is not None and exp is not None
            and result.get("avg_cost_per_kwh_mtd") is not None
            and result.get("mtd_load_kwh", 0) > 0 else None)
        return result
