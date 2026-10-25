"""Display-only export bonuses, conditional credits and fixed supply costs.

This accounting never feeds an optimizer objective or a hardware command.
Only observed intervals earn a bonus. Unknown quota history withholds totals
until the next complete local-day window rather than inventing earnings.
ZeroHero credits remain unverified until complete next-day portal cost data.
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
    tracker = DailyCostTracker(config, configured_supply_charge(entry, now), state)
    portal = getattr(entry_data.get("globird_coordinator"), "data", None) or {}
    # No service selector exists yet. Multiple sites must never settle another
    # home's credit against this home's measured costs.
    services = list((portal.get("service_data") or {}).values())
    if config is not None and len(services) == 1:
        tracker.reconcile_credit(services[0].get("cost_summary") or {}, now)
    return tracker


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
        self.state["credits"] = {
            day: dict(record) for day, record in (self.state.get("credits") or {}).items()
        }

    def reconcile_credit(self, summary, now):
        """Replace estimates with signed, complete, past-day portal credits."""
        grouped = {}
        for row in summary.get("daily", []):
            day = str(row.get("date") or "").split("T")[0].replace("/", "-")
            if not row.get("complete") or day >= now.date().isoformat():
                continue
            grouped.setdefault(day, []).append(row)
        for day, rows in grouped.items():
            if day[:7] != now.strftime("%Y-%m"):
                continue
            # Portal costs are signed: a negative credit reduces net cost.
            amounts = [_finite(row.get("amount"), float("nan")) for row in rows
                       if str(row.get("chargeCategory") or "").strip().casefold()
                       == "zerohero credit"]
            if not all(math.isfinite(amount) for amount in amounts):
                continue
            amount = -sum(amounts)
            self.state["credits"][day] = {
                "amount": amount, "status": "confirmed" if amount else "not_awarded",
                "source": "globird_api",
            }

    def _observe_credit(self, start, end, import_kw, integrated):
        """Accrue only a fully observed window; eligibility stays unverified."""
        config = self.config
        if config is None:
            return
        credits = self.state["credits"]
        # Keep the prior month across midnight for overnight windows, but no
        # unbounded daily history in HA storage.
        cutoff = (end.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        for day in list(credits):
            if day[:7] < cutoff:
                del credits[day]
        if start is not None and _seconds(start, end) > 0:
            cursor = start
            for day, active, hours in _parts(start, end, config):
                if active:
                    first, last = config.start, config.end
                    if last <= first and cursor.strftime("%H:%M") < last:
                        day = (cursor - timedelta(days=1)).date().isoformat()
                    record = credits.setdefault(day, {"hours": 0.0, "import_kwh": 0.0})
                    signature = [config.start, config.end, config.credit_amount,
                                 config.import_limit_kw]
                    if record.get("signature", signature) != signature:
                        record["invalid"] = True
                    record["signature"] = signature
                    power = _finite(import_kw, float("nan"))
                    if integrated and math.isfinite(power):
                        record["hours"] = record.get("hours", 0.0) + hours
                        record["import_kwh"] = record.get("import_kwh", 0.0) + max(0.0, power) * hours
                cursor = datetime.fromtimestamp(cursor.timestamp() + hours * 3600, cursor.tzinfo)
        day = end.date().isoformat()
        credits.setdefault(day, {"hours": 0.0, "import_kwh": 0.0})
        for day, record in credits.items():
            if record.get("source") == "globird_api":
                continue
            if record.get("status") in ("unverified", "unknown", "missed"):
                continue
            midnight = datetime.fromisoformat(day).replace(tzinfo=end.tzinfo)
            h, m = map(int, config.end.split(":"))
            finish = midnight.replace(hour=h, minute=m)
            if config.end <= config.start:
                finish += timedelta(days=1)
            begin_h, begin_m = map(int, config.start.split(":"))
            begin = midnight.replace(hour=begin_h, minute=begin_m)
            expected = _seconds(begin, finish) / 3600
            complete = (not record.get("invalid")
                        and abs(record.get("hours", 0.0) - expected) < 1e-6)
            status = "pending"
            if end >= finish:
                if not complete:
                    status = "unknown"
                elif record.get("import_kwh", 0.0) > config.import_allowance_kwh + 1e-6:
                    status = "missed"
                else:
                    status = "unverified"
            record.update(status=status, amount=config.credit_amount if status == "unverified" else 0.0)

    def _begin(self, now):
        day = now.date().isoformat()
        month = day[:7]
        if self.state.get("month") != month:
            unknown_prior = self.state.get("unknown_prior_bonus_month") == month
            self.state = {"month": month, "supply_by_day": {}, "mtd_bonus": 0.0,
                          "credits": self.state.get("credits", {}),
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

    def observe(self, start, end, export_kw, base_price, integrated, import_kw=None):
        self._observe_credit(start, end, import_kw, integrated)
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
        credits = self.state.get("credits", {}) if self.config is not None else {}
        today_credit = credits.get(self.state.get("day"), {})
        credit = today_credit.get("amount", 0.0)
        mtd_credit = sum(record.get("amount", 0.0) for day, record in credits.items()
                         if day[:7] == self.state.get("month"))
        net = (round(cost - earnings + daily_supply - credit, 4)
               if cost is not None and earnings is not None else None)
        result.update(daily_supply_charge=round(daily_supply, 4), net_cost_today=net,
                      export_bonus_today=round(self.state.get("bonus", 0.0), 4),
                      export_bonus_coverage="partial" if partial else "complete",
                      mtd_supply_charge=round(mtd_supply, 4),
                      supply_charge_days=elapsed_days)
        result.update(zerohero_credit_today=round(credit, 4),
                      zerohero_credit_status=today_credit.get("status", "disabled"),
                      zerohero_credit_verification="GloBird complete next-day API cost data required",
                      mtd_zerohero_credit=round(mtd_credit, 4),
                      zerohero_credit_days={day: {"amount": record.get("amount", 0.0),
                                                "status": record.get("status", "pending")}
                                           for day, record in credits.items()
                                           if day[:7] == self.state.get("month")})
        if partial:
            result["export_earnings_coverage"] = "partial"
        result["avg_cost_per_kwh_today"] = (
            round(net / result["load_today_kwh"], 4)
            if net is not None and result.get("avg_cost_per_kwh_today") is not None
            and result.get("load_today_kwh", 0) > 0 else None)
        imp, exp = result.get("mtd_import_cost"), result.get("mtd_export_earnings")
        result["avg_cost_per_kwh_mtd"] = (
            round((imp - exp + mtd_supply - mtd_credit) / result["mtd_load_kwh"], 4)
            if imp is not None and exp is not None
            and result.get("avg_cost_per_kwh_mtd") is not None
            and result.get("mtd_load_kwh", 0) > 0 else None)
        return result
