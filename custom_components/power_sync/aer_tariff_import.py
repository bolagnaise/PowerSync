"""Convert a published AER electricity plan into an editable PowerSync tariff.

``import_prd_electricity_plan`` reads a Product Reference Data (Energy Made
Easy / Victorian Energy Compare) plan and returns a neutral schedule: standard
import and export rates, time-windowed rates, kWh allowances and warnings.

PRD usage and supply prices are dollars excluding GST; feed-in tariffs are
GST-free dollars. Schedules use GST-inclusive cents. PRD end times are
inclusive minutes ("20:59"); schedules use exclusive half-hour boundaries.

``schedule_to_tariff_draft`` then maps that schedule onto the existing custom
tariff editor: calendar seasons, non-overlapping periods with an import and an
export price, the standard rates, the daily supply charge and the daily
free-import and premium-export allowances. The result is a starting point for owner review, never a saved
tariff.
"""

from __future__ import annotations

import math
import re
from typing import Any

GST = 1.1
DAY_NUMBERS = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
SLOTS_PER_DAY = 48
MONTH_ABBREVIATIONS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
_NUMBER = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")
_TIME = re.compile(r"^(\d{2}):?(\d{2})$")


class PrdImportError(ValueError):
    """The published plan has no usable electricity contract."""


def _round(value: float) -> float:
    """Round to 0.001 the way the Cloud importer does (half away from zero)."""
    scaled = value * 1000
    return math.floor(scaled + 0.5) / 1000 if scaled >= 0 else -math.floor(-scaled + 0.5) / 1000


def _number_text(value: float) -> str:
    """Format a price like JavaScript does: 27.5, 1, 0.25."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


def _truthy(value: Any) -> bool:
    """JavaScript truthiness: empty lists and objects are truthy."""
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not math.isnan(value)
    if isinstance(value, str):
        return value != ""
    return True


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, dict) else []


def _text(value: Any, fallback: str = "") -> str:
    return value.strip()[:60] if isinstance(value, str) and value.strip() else fallback


def _dollars(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and _NUMBER.match(value.strip()):
        number = float(value.strip())
    else:
        return None
    return number if math.isfinite(number) else None


def _minute(value: Any, end: bool, warnings: dict[str, None]) -> int | None:
    match = _TIME.match(value.strip()) if isinstance(value, str) else None
    if not match:
        return None
    total = int(match.group(1)) * 60 + int(match.group(2))
    if end and total % 30 == 29:
        total += 1
    if total % 30:
        warnings[
            "Some published times are not on half-hour boundaries and were "
            "rounded to the nearest half hour."
        ] = None
        total = math.floor(total / 30 + 0.5) * 30
    if end and total == 0:
        total = 1440
    return None if total > 1440 or (not end and total == 1440) else total


def _days(value: Any) -> list[int] | None:
    numbers = sorted({DAY_NUMBERS[str(day)] for day in _list(value) if str(day) in DAY_NUMBERS})
    return None if not numbers or len(numbers) == 7 else numbers


def _blocks(value: Any, gst: bool) -> list[dict[str, Any]]:
    blocks = []
    for raw in _list(value):
        block = _record(raw)
        price = _dollars(block.get("unitPrice"))
        volume = _dollars(block.get("volume"))
        if price is None:
            continue
        blocks.append(
            {
                "priceC": _round(price * 100 * (GST if gst else 1)),
                "volume": volume if volume is not None and volume > 0 else None,
            }
        )
    return blocks


def _windows(value: Any, warnings: dict[str, None]) -> list[dict[str, Any]]:
    windows = []
    for raw in _list(value):
        window = _record(raw)
        start = _minute(window.get("startTime"), False, warnings)
        end = _minute(window.get("endTime"), True, warnings)
        if start is None or end is None or start == end:
            continue
        raw_days = _list(window.get("days"))
        if raw_days and not any(str(day) in DAY_NUMBERS for day in raw_days):
            continue
        selected = _days(window.get("days"))
        windows.append(
            {"startMinute": start, "endMinute": end, **({"days": selected} if selected else {})}
        )
    return windows


def _coverage(entry: dict[str, Any]) -> int:
    if entry["windows"] is None:
        return 1440 * 7
    total = 0
    for window in entry["windows"]:
        span = (window["endMinute"] - window["startMinute"] + 1440) % 1440 or 1440
        total += span * len(window.get("days") or range(7))
    return total


def _season_months(start: Any, end: Any) -> list[int] | None:
    """Months (1-12) whose middle day falls inside a PRD MM-DD season."""

    def parse(value: Any) -> int | None:
        if isinstance(value, str) and re.fullmatch(r"\d{2}-\d{2}", value):
            return int(value[:2]) * 100 + int(value[3:])
        return None

    first, last = parse(start), parse(end)
    if first is None or last is None:
        return None
    months = []
    for month in range(1, 13):
        middle = month * 100 + 15
        inside = first <= middle <= last if first <= last else middle >= first or middle <= last
        if inside:
            months.append(month)
    return months or None


def _volume_period(value: Any) -> str:
    return "day" if value is None or value == "P1D" else "other"


def _import_entries(
    contract: dict[str, Any], warnings: dict[str, None], supply: list[float]
) -> list[dict[str, Any]]:
    seasons = [_record(season) for season in _list(contract.get("tariffPeriod"))]
    seasonal = len(seasons) > 1
    entries: list[dict[str, Any]] = []
    for season in seasons:
        months = _season_months(season.get("startDate"), season.get("endDate")) if seasonal else None
        if seasonal and not months:
            warnings["A seasonal tariff period could not be matched to calendar months."] = None
        raw_charge = season.get("dailySupplyCharge")
        charge = _dollars(raw_charge if raw_charge is not None else season.get("dailySupplyCharges"))
        if charge is not None:
            supply.append(_round(charge * 100 * GST))
        extra = {"months": months} if months else {}
        block_type = season.get("rateBlockUType")
        if block_type == "timeOfUseRates" or isinstance(season.get("timeOfUseRates"), list):
            for raw in _list(season.get("timeOfUseRates")):
                rate = _record(raw)
                found = _windows(rate.get("timeOfUse"), warnings)
                if not found:
                    warnings[
                        f"{_text(rate.get('displayName'), 'A usage period')} has no "
                        "readable times and was skipped."
                    ] = None
                    continue
                entries.append(
                    {
                        "label": _text(rate.get("displayName"), _text(rate.get("type"), "Usage")),
                        "blocks": _blocks(rate.get("rates"), True),
                        "windows": found,
                        "volumePeriod": _volume_period(rate.get("period")),
                        **extra,
                    }
                )
        elif block_type == "singleRate" or _truthy(season.get("singleRate")):
            rate = _record(season.get("singleRate"))
            entries.append(
                {
                    "label": _text(rate.get("displayName"), "All day"),
                    "blocks": _blocks(rate.get("rates"), True),
                    "windows": None,
                    "volumePeriod": _volume_period(rate.get("period")),
                    **extra,
                }
            )
        elif block_type == "demandCharges" or _truthy(season.get("demandCharges")):
            warnings["Demand charges are not modelled. Enter the energy rates from your bill."] = None
        if _truthy(season.get("demandCharges")) and block_type != "demandCharges":
            warnings["Demand charges are not modelled."] = None
    return [entry for entry in entries if entry["blocks"]]


def _export_entries(contract: dict[str, Any], warnings: dict[str, None]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for raw in _list(contract.get("solarFeedInTariff")):
        tariff = _record(raw)
        label = _text(tariff.get("displayName"), "Feed-in")
        if tariff.get("scheme") == "PREMIUM":
            warnings[
                f"{label} is a closed government premium scheme and was skipped. "
                "Add it only if your account receives it."
            ] = None
            continue
        if tariff.get("tariffUType") == "singleTariff" or _truthy(tariff.get("singleTariff")):
            single = _record(tariff.get("singleTariff"))
            rates = single.get("rates")
            entries.append(
                {
                    "label": label,
                    "blocks": _blocks(rates if rates is not None else [single], False),
                    "windows": None,
                    "volumePeriod": "day",
                }
            )
        for raw_varying in _list(tariff.get("timeVaryingTariffs")):
            varying = _record(raw_varying)
            found = _windows(varying.get("timeVariations"), warnings)
            rates = varying.get("rates")
            priced = _blocks(rates if rates is not None else [varying], False)
            if not found or not priced:
                continue
            entries.append(
                {
                    "label": _text(varying.get("displayName"), label),
                    "blocks": priced,
                    "windows": found,
                    "volumePeriod": "day",
                }
            )
    # Matches the Cloud importer, which also counts a tariff without a
    # readable price here.
    if any(not entry["blocks"] or entry["blocks"][0]["volume"] is not None for entry in entries):
        warnings[
            "Feed-in allowances were read as daily limits. Confirm the limit "
            "period on your contract."
        ] = None
    return [entry for entry in entries if entry["blocks"]]


def _build(direction: str, entries: list[dict[str, Any]], warnings: dict[str, None]) -> dict[str, Any]:
    rates: list[dict[str, Any]] = []
    allowances: list[dict[str, Any]] = []
    priced = [entry for entry in entries if entry["blocks"][-1]["volume"] is None]
    seasons = {",".join(map(str, entry.get("months") or [])) for entry in priced}
    covered = sum(_coverage(entry) for entry in priced) / max(1, len(seasons))
    complete = covered >= 1440 * 7
    # Unpublished export hours must not inherit a premium window's rate.
    if not complete and direction == "export":
        ordered = sorted(priced, key=lambda entry: entry["blocks"][-1]["priceC"])
    else:
        ordered = sorted(
            priced,
            key=lambda entry: (-_coverage(entry), 1 if entry.get("months") else 0),
        )
    base = ordered[0] if ordered else None
    base_price = base["blocks"][-1]["priceC"] if base else None
    if base and not complete:
        warnings[f"Times without a published {direction} rate use {_number_text(base_price)}c/kWh."] = None
    for entry in entries:
        balance = entry["blocks"][-1]
        quota = entry["blocks"][0] if len(entry["blocks"]) > 1 or balance["volume"] is not None else None
        if len(entry["blocks"]) > 2:
            # Only the first block (as an allowance) and the final rate are
            # modelled; say so rather than silently repricing the middle tiers.
            warnings[
                f"{entry['label']}: usage tiers between the first block and the "
                "final rate were ignored; check the tiered rates on your contract."
            ] = None
        if quota and quota["volume"]:
            shape = entry["windows"] or [{"startMinute": 0, "endMinute": 1440}]
            if entry["volumePeriod"] != "day":
                warnings[f"{entry['label']}: a non-daily usage block was ignored; the higher block rate applies."] = None
            elif len(shape) != 1 or shape[0].get("days"):
                warnings[f"{entry['label']}: an allowance shared across several windows was not applied."] = None
            else:
                duplicate = next(
                    (
                        rule
                        for rule in allowances
                        if rule["startMinute"] == shape[0]["startMinute"]
                        and rule["endMinute"] == shape[0]["endMinute"]
                        and rule["limitKwh"] == quota["volume"]
                        and rule["priceC"] == quota["priceC"]
                    ),
                    None,
                )
                if duplicate is not None:
                    if duplicate.get("months") and entry.get("months"):
                        duplicate["months"] = sorted({*duplicate["months"], *entry["months"]})
                    if not entry.get("months") or len(duplicate.get("months") or []) == 12:
                        duplicate.pop("months", None)
                elif any(rule["direction"] == direction for rule in allowances):
                    warnings[f"{entry['label']}: only one {direction} allowance can apply; review the allowances."] = None
                else:
                    allowances.append(
                        {
                            "id": f"{direction}_allowance",
                            "direction": direction,
                            "startMinute": shape[0]["startMinute"],
                            "endMinute": shape[0]["endMinute"],
                            "limitKwh": quota["volume"],
                            "priceC": quota["priceC"],
                            "period": "day",
                            "label": entry["label"],
                            **({"months": list(entry["months"])} if entry.get("months") else {}),
                        }
                    )
        if balance["volume"] is not None or entry is base or balance["priceC"] == base_price:
            continue
        for window in entry["windows"] or [{"startMinute": 0, "endMinute": 1440}]:
            rates.append(
                {
                    "direction": direction,
                    **window,
                    "priceC": balance["priceC"],
                    "label": entry["label"],
                    **({"months": list(entry["months"])} if entry.get("months") else {}),
                }
            )
    return {"basePriceC": base_price, "rates": _merge_seasons(rates), "allowances": allowances}


def _merge_seasons(rates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join identical windows that only differ by season, e.g. summer and winter peaks."""
    merged: list[dict[str, Any]] = []
    for rate in rates:
        twin = next(
            (
                other
                for other in merged
                if other.get("months")
                and rate.get("months")
                and other["direction"] == rate["direction"]
                and other["startMinute"] == rate["startMinute"]
                and other["endMinute"] == rate["endMinute"]
                and other["priceC"] == rate["priceC"]
                and other.get("days") == rate.get("days")
            ),
            None,
        )
        if twin is None:
            merged.append(dict(rate))
            continue
        twin["months"] = sorted({*twin["months"], *rate["months"]})
        if len(twin["months"]) == 12:
            del twin["months"]
    return merged


def import_prd_electricity_plan(plan: Any) -> dict[str, Any]:
    """Convert one PRD plan detail (``data`` of the plan endpoint)."""
    value = _record(plan)
    contract = _record(value.get("electricityContract"))
    if not contract:
        raise PrdImportError("prd_electricity_contract_missing")
    warnings: dict[str, None] = {}
    supply: list[float] = []
    import_rules = _build("import", _import_entries(contract, warnings, supply), warnings)
    export_rules = _build("export", _export_entries(contract, warnings), warnings)
    if import_rules["basePriceC"] is None:
        raise PrdImportError("prd_usage_rates_missing")
    if len(set(supply)) > 1:
        warnings["The supply charge differs by season; the first season’s charge was used."] = None
    if _list(contract.get("controlledLoad")):
        warnings["Controlled-load rates are excluded; they are billed separately."] = None
    if contract.get("pricingModel") == "QUOTA":
        warnings["This is a quota-based plan. Confirm the included usage against your contract."] = None
    for incentive in _list(contract.get("incentives")):
        name = _text(_record(incentive).get("displayName"))
        if name:
            warnings[f"Incentive to confirm: {name}."] = None
    if export_rules["basePriceC"] is None:
        warnings["No feed-in tariff was published; export was set to 0c/kWh."] = None
    effective = value.get("effectiveFrom")
    effective = effective[:10] if isinstance(effective, str) else None
    plan_id = value.get("planId")
    return {
        "sourcePlanId": "" if plan_id is None else str(plan_id),
        "label": _text(value.get("displayName"), "Public plan"),
        "clock": "aest" if contract.get("timeZone") == "AEST" else "local",
        "effectiveFrom": effective if effective and re.fullmatch(r"\d{4}-\d{2}-\d{2}", effective) else None,
        "schedule": {
            "baseImportC": import_rules["basePriceC"],
            "baseExportC": export_rules["basePriceC"] if export_rules["basePriceC"] is not None else 0,
            "dailySupplyC": supply[0] if supply else 0,
            "rates": [*import_rules["rates"], *export_rules["rates"]],
            "allowances": [*import_rules["allowances"], *export_rules["allowances"]],
        },
        "warnings": list(warnings),
    }


# ---------------------------------------------------------------------------
# Schedule -> custom tariff editor draft
# ---------------------------------------------------------------------------


def _window_contains(rule: dict[str, Any], minute: int, weekday: int, month: int) -> bool:
    """Cloud evaluator semantics: days/months filter the local instant."""
    start, end = rule["startMinute"], rule["endMinute"]
    inside = start <= minute < end if start < end else minute >= start or minute < end
    return (
        inside
        and (not rule.get("days") or weekday in rule["days"])
        and (not rule.get("months") or month in rule["months"])
    )


def _raster(schedule: dict[str, Any], month: int) -> tuple[tuple[Any, ...], ...]:
    """Per day (Tesla 0=Sunday) and half hour: (import c, export c, label)."""
    base_import = schedule["baseImportC"]
    base_export = schedule["baseExportC"]
    days = []
    for weekday in range(7):
        slots = []
        for slot in range(SLOTS_PER_DAY):
            minute = slot * 30
            import_c, export_c = base_import, base_export
            import_label = export_label = None
            # Later rules override earlier ones.
            for rule in schedule.get("rates", []):
                if not _window_contains(rule, minute, weekday, month):
                    continue
                if rule["direction"] == "import":
                    import_c, import_label = rule["priceC"], rule.get("label")
                else:
                    export_c, export_label = rule["priceC"], rule.get("label")
            label = import_label if import_c != base_import else export_label
            slots.append((import_c, export_c, label or ""))
        days.append(tuple(slots))
    return tuple(days)


def _month_runs(months: list[int]) -> list[list[int]]:
    """Split a month set into contiguous runs, joining December to January."""
    selected = sorted(set(months))
    if len(selected) == 12:
        return [list(range(1, 13))]
    runs: list[list[int]] = []
    for month in selected:
        if runs and runs[-1][-1] == month - 1:
            runs[-1].append(month)
        else:
            runs.append([month])
    if len(runs) > 1 and runs[0][0] == 1 and runs[-1][-1] == 12:
        runs[0] = runs.pop() + runs[0]
    return runs


def _season_name(run: list[int]) -> str:
    if len(run) == 12:
        return "All Year"
    first, last = MONTH_ABBREVIATIONS[run[0] - 1], MONTH_ABBREVIATIONS[run[-1] - 1]
    return first if len(run) == 1 else f"{first}–{last}"


def _day_value(days: list[int]) -> str | list[int]:
    if len(days) == 7:
        return "all_days"
    if days == [1, 2, 3, 4, 5]:
        return "weekdays"
    if days == [0, 6]:
        return "weekends"
    return list(days)


def _hour_value(slot: int) -> int | float:
    hours = slot / 2
    return int(hours) if hours.is_integer() else hours


def _slot_runs(row: tuple[bool, ...], wrap: bool) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    start = None
    for slot in range(SLOTS_PER_DAY + 1):
        active = slot < SLOTS_PER_DAY and row[slot]
        if active and start is None:
            start = slot
        elif not active and start is not None:
            runs.append((start, slot))
            start = None
    if wrap and len(runs) > 1 and runs[0][0] == 0 and runs[-1][1] == SLOTS_PER_DAY:
        runs = [(runs[-1][0], runs[0][1]), *runs[1:-1]]
    return runs


def _period_type(import_c: float, export_c: float, base_import: float, base_export: float,
                 above: list[float], below: list[float]) -> str:
    """Map a price level onto the editor's TOU period types."""
    if import_c > base_import:
        return "PEAK" if import_c == max(above) else "SHOULDER"
    if import_c < base_import:
        if import_c <= 0 or (len(below) > 1 and import_c == min(below)):
            return "SUPER_OFF_PEAK"
        return "OFF_PEAK"
    return "PEAK" if export_c > base_export else "OFF_PEAK"


def _season_periods(
    raster: tuple[tuple[Any, ...], ...], base_import: float, base_export: float
) -> list[dict[str, Any]]:
    keys: list[tuple[Any, ...]] = []
    for day in raster:
        for value in day:
            if (value[0], value[1]) != (base_import, base_export) and value not in keys:
                keys.append(value)
    above = sorted({key[0] for key in keys if key[0] > base_import})
    below = sorted({key[0] for key in keys if key[0] < base_import})
    periods = []
    for key in keys:
        rows: dict[tuple[bool, ...], list[int]] = {}
        for weekday, day in enumerate(raster):
            row = tuple(value == key for value in day)
            if any(row):
                rows.setdefault(row, []).append(weekday)
        for row, days in rows.items():
            for start, end in _slot_runs(row, wrap=len(days) == 7):
                import_c, export_c, label = key
                periods.append(
                    {
                        "name": _period_type(import_c, export_c, base_import, base_export, above, below),
                        **({"label": label} if label else {}),
                        "start": _hour_value(start),
                        "end": _hour_value(end),
                        "days": _day_value(days),
                        "import_rate": None if import_c == base_import else round(import_c / 100, 6),
                        "export_rate": None if export_c == base_export else round(export_c / 100, 6),
                    }
                )
    periods.sort(key=lambda period: (period["start"], str(period["days"])))
    return periods


def _minute_text(minute: int) -> str:
    """Runtime quota windows spell midnight 00:00 (an end of 00:00 wraps)."""
    minute %= 1440
    return f"{minute // 60:02d}:{minute % 60:02d}"


def _import_allowance(
    schedule: dict[str, Any], rasters: dict[int, Any], timezone_token: str | None,
    warnings: list[str],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Map daily allowances onto the custom tariff import and export quotas.

    A free or discounted import allowance becomes ``import_quota``: the
    discount is measured from the lowest usual rate in its window. A premium
    feed-in allowance becomes ``export_quota``: the premium is measured from
    the highest usual rate in its window. Both choices keep the modelled
    benefit at or below the real one.
    """
    quotas: dict[str, dict[str, Any] | None] = {"import": None, "export": None}
    for allowance in schedule.get("allowances", []):
        label = allowance.get("label") or "Allowance"
        direction = "export" if allowance["direction"] == "export" else "import"
        if allowance.get("period", "day") != "day":
            warnings.append(f"{label}: only daily allowances are supported; it was not applied.")
            continue
        if allowance.get("months"):
            warnings.append(f"{label}: a seasonal allowance can't be applied and was skipped.")
            continue
        if quotas[direction] is not None:
            warnings.append(f"{label}: only one {direction} allowance can apply; it was skipped.")
            continue
        start, end = allowance["startMinute"], allowance["endMinute"]
        price_index = 0 if direction == "import" else 1
        window_prices = {
            day[slot][price_index]
            for raster in rasters.values()
            for day in raster
            for slot in range(SLOTS_PER_DAY)
            if (start <= slot * 30 < end if start < end else slot * 30 >= start or slot * 30 < end)
        }
        if direction == "import":
            underlying = min(window_prices) if window_prices else schedule["baseImportC"]
            benefit = round(underlying - allowance["priceC"], 6)
            varies = "the allowance discount uses the lowest rate"
            skipped = "the allowance is not cheaper than the usual rate and was skipped"
        else:
            underlying = max(window_prices) if window_prices else schedule["baseExportC"]
            benefit = round(allowance["priceC"] - underlying, 6)
            varies = "the allowance premium uses the highest rate"
            skipped = "the allowance pays no more than the usual feed-in rate and was skipped"
        if len(window_prices) > 1:
            warnings.append(
                f"{label}: the usual rate varies inside the allowance window; "
                f"{varies}, {_number_text(underlying)}c/kWh."
            )
        if benefit <= 0:
            warnings.append(f"{label}: {skipped}.")
            continue
        quotas[direction] = {
            "enabled": True,
            "name": label,
            "windows": [[_minute_text(start), _minute_text(end)]],
            "daily_cap_kwh": float(allowance["limitKwh"]),
            "base_price_c_per_kwh": round(underlying, 6),
            "bonus_price_c_per_kwh": benefit,
            **({"timezone_token": timezone_token} if timezone_token else {}),
        }
    return quotas["import"], quotas["export"]


def schedule_to_tariff_draft(
    imported: dict[str, Any],
    *,
    utility: str,
    region: str | None = None,
    network: str | None = None,
    retailer: str | None = None,
) -> dict[str, Any]:
    """Map an imported schedule onto the custom tariff editor's state."""
    from .aer_plans import DAYLIGHT_SAVING_REGIONS

    schedule = imported["schedule"]
    warnings = list(imported.get("warnings", []))
    base_import = schedule["baseImportC"]
    base_export = schedule["baseExportC"]

    rasters = {month: _raster(schedule, month) for month in range(1, 13)}
    groups: dict[Any, list[int]] = {}
    for month, raster in rasters.items():
        groups.setdefault(raster, []).append(month)
    seasons: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    for raster, months in groups.items():
        season_periods = _season_periods(raster, base_import, base_export)
        for run in _month_runs(months):
            name = _season_name(run)
            seasons.append({"name": name, "from_month": run[0], "to_month": run[-1]})
            for period in season_periods:
                periods.append({**period, **({"season": name} if len(groups) > 1 else {})})
    seasons.sort(key=lambda season: season["from_month"])
    season_order = {season["name"]: index for index, season in enumerate(seasons)}
    periods.sort(key=lambda period: season_order.get(period.get("season", "All Year"), 0))

    aest = imported.get("clock") == "aest"
    if aest and region in DAYLIGHT_SAVING_REGIONS:
        warnings.append(
            "This plan publishes its times in AEST. During daylight saving they "
            "fall one hour later on the local clock; check the periods."
        )
    quota, export_quota = _import_allowance(
        schedule, rasters, "Australia/Brisbane" if aest else None, warnings
    )
    return {
        "plan_name": imported.get("label") or "Public plan",
        "utility": utility,
        "standard_import_rate": round(base_import / 100, 6),
        "standard_export_rate": round(base_export / 100, 6),
        "daily_supply_charge": round(schedule.get("dailySupplyC", 0) / 100, 6),
        "seasons": seasons,
        "periods": periods,
        "import_quota": quota,
        "export_quota": export_quota,
        "source": {
            "kind": "aer_prd",
            "plan_id": imported.get("sourcePlanId") or None,
            "effective_from": imported.get("effectiveFrom"),
            **({"retailer": retailer} if retailer else {}),
            **({"region": region} if region else {}),
            **({"network": network} if network else {}),
        },
        "warnings": warnings,
    }
