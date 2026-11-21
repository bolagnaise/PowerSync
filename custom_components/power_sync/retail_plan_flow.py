"""Shared helpers for the retail plan selector and custom tariff period table.

The setup and options flows both offer: retailer, then state and network, then
a plan source (published AER plan, PowerSync preset, or manual entry), then the
editable period table. These helpers hold the pure parts — selector options,
period normalisation, preset conversion and the Markdown table shown above the
period form — so both flows stay thin and behave identically.

Periods use the custom tariff editor's existing row format::

    {"name": "PEAK", "label": "Peak", "start": 15, "end": 21.5,
     "days": "weekdays" | "weekends" | "all_days" | [0, 6],
     "import_rate": 0.55 | None, "export_rate": 0.04 | None,
     "season": "Summer"}

Times are hours on half-hour boundaries (24 = midnight end). Rates are major
currency units per kWh; ``None`` means the standard rate applies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .aer_plans import (
    ELECTRICITY_NETWORKS,
    ELECTRICITY_PROVIDER_DIRECTORY,
    PLAN_REGIONS,
    RetailerEntry,
    network_option_value,
)

SOURCE_PUBLISHED = "published"
SOURCE_PRESET = "preset"
SOURCE_MANUAL = "manual"
SOURCE_EDIT = "edit"
SOURCE_CHOOSE = "choose"
MANUAL_PLAN_CHOICE = "__manual__"

PERIOD_TYPES = ("PEAK", "SHOULDER", "OFF_PEAK", "SUPER_OFF_PEAK")
PERIOD_TYPE_LABELS = {
    "PEAK": "Peak",
    "SHOULDER": "Shoulder",
    "OFF_PEAK": "Off-Peak",
    "SUPER_OFF_PEAK": "Super Off-Peak",
}
DAY_PRESETS = {
    "weekdays": [1, 2, 3, 4, 5],
    "weekends": [0, 6],
    "all_days": [0, 1, 2, 3, 4, 5, 6],
}
DAY_ABBREVIATIONS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
# Monday-first order for selectors; values are Tesla day numbers (0=Sunday).
DAY_SELECTOR_ORDER = (1, 2, 3, 4, 5, 6, 0)
DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DEDICATED_PROVIDER_NAMES = {
    "amber": "Amber Electric",
    "flow_power": "Flow Power",
    "globird": "GloBird",
    "covau": "CovaU SolarMax",
    "localvolts": "Localvolts",
    "agl": "AGL Battery Rewards",
}
PRICE_STRIP_LEVELS = "▁▂▃▄▅▆▇█"


# --- selector options -------------------------------------------------------


def retailer_options() -> list[tuple[str, str]]:
    """Retailers in directory order (value, label)."""
    return [(entry.id, entry.label) for entry in ELECTRICITY_PROVIDER_DIRECTORY]


def network_options() -> list[tuple[str, str]]:
    """Every state/network pair as one selector option."""
    return [
        (network_option_value(region, network), f"{label} — {network}")
        for region, label in PLAN_REGIONS.items()
        for network in ELECTRICITY_NETWORKS[region]
    ]


def source_options(entry: RetailerEntry | None, has_presets: bool) -> list[tuple[str, str]]:
    """Plan sources available for one retailer."""
    options: list[tuple[str, str]] = []
    if entry is not None and entry.prd:
        options.append((SOURCE_PUBLISHED, "Published plan from Energy Made Easy (AER)"))
    if has_presets:
        options.append((SOURCE_PRESET, "PowerSync preset"))
    options.append((SOURCE_MANUAL, "Enter rates manually"))
    return options


def dedicated_provider_hint(entry: RetailerEntry | None) -> str:
    """Point retailers with live pricing at their own provider option."""
    if entry is None or entry.provider not in DEDICATED_PROVIDER_NAMES:
        return ""
    return (
        f"PowerSync also has a dedicated {DEDICATED_PROVIDER_NAMES[entry.provider]} "
        "electricity provider with live or reviewed pricing. Choose it as the "
        "electricity provider instead if your plan is covered there."
    )


def half_hour_options(*, end: bool = False) -> list[tuple[str, str]]:
    """Half-hour clock values. End times also offer 24:00 (midnight)."""
    values = [f"{slot // 2:02d}:{30 * (slot % 2):02d}" for slot in range(48)]
    if end:
        values = values[1:] + ["24:00"]
    return [(value, value) for value in values]


def day_selector_options() -> list[tuple[str, str]]:
    return [(str(day), DAY_NAMES[day]) for day in DAY_SELECTOR_ORDER]


def month_selector_options() -> list[tuple[str, str]]:
    names = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    return [(str(index), name) for index, name in enumerate(names, 1)]


# --- normalisation ----------------------------------------------------------


def parse_clock(value: Any, *, end: bool = False) -> int | float | None:
    """Parse "HH:MM" on a half-hour boundary into hours (15:30 -> 15.5)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        hours = float(value)
    elif isinstance(value, str) and ":" in value:
        hour_text, minute_text = value.strip().split(":", 1)
        try:
            hour, minute = int(hour_text), int(minute_text)
        except ValueError:
            return None
        if minute not in (0, 30) or not 0 <= hour <= 24:
            return None
        hours = hour + minute / 60
    else:
        return None
    # An end of 0 is the legacy spelling of midnight; a start of 24 is invalid.
    if hours * 2 != int(hours * 2) or not 0 <= hours <= (24 if end else 23.5):
        return None
    return int(hours) if hours.is_integer() else hours


def clock_text(hours: Any) -> str:
    """Format editor hours as "HH:MM"."""
    try:
        value = float(hours)
    except (TypeError, ValueError):
        value = 0.0
    minutes = int(round(value * 60))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_days(value: Any) -> str | list[int] | None:
    """Accept a preset name or a list of Tesla day numbers (0=Sunday)."""
    if isinstance(value, str):
        return value if value in DAY_PRESETS else None
    if not isinstance(value, (list, tuple, set)):
        return None
    days: set[int] = set()
    for day in value:
        try:
            number = int(day)
        except (TypeError, ValueError):
            return None
        if not 0 <= number <= 6:
            return None
        days.add(number)
    if not days:
        return None
    ordered = sorted(days)
    for name, preset in DAY_PRESETS.items():
        if ordered == preset:
            return name
    return ordered


def day_numbers(value: Any) -> list[int]:
    """Tesla day numbers for a stored period ``days`` value."""
    normalized = normalize_days(value)
    if normalized is None:
        return list(DAY_PRESETS["weekdays"])
    if isinstance(normalized, str):
        return list(DAY_PRESETS[normalized])
    return normalized


def day_ranges(value: Any) -> list[tuple[int, int]]:
    """Contiguous inclusive Tesla day ranges for a period's days."""
    if value == "weekdays":
        return [(1, 5)]
    if value == "weekends":
        return [(0, 0), (6, 6)]
    if value == "all_days":
        return [(0, 6)]
    ranges: list[tuple[int, int]] = []
    for day in day_numbers(value):
        if ranges and ranges[-1][1] == day - 1:
            ranges[-1] = (ranges[-1][0], day)
        else:
            ranges.append((day, day))
    return ranges


def days_text(value: Any) -> str:
    normalized = normalize_days(value)
    if normalized == "weekdays":
        return "Mon-Fri"
    if normalized == "weekends":
        return "Sat-Sun"
    if normalized == "all_days" or normalized is None:
        return "Mon-Sun"
    ordered = sorted(normalized, key=DAY_SELECTOR_ORDER.index)
    return ", ".join(DAY_ABBREVIATIONS[day] for day in ordered)


def optional_rate(value: Any) -> float | None:
    """Convert a minor-unit form value to major units; blank means standard."""
    if value is None or value == "":
        return None
    try:
        return float(value) / 100
    except (TypeError, ValueError):
        return None


def normalize_period(period: Any) -> dict[str, Any] | None:
    """Validate one saved or imported period row, failing closed."""
    if not isinstance(period, dict):
        return None
    name = str(period.get("name", ""))
    start = parse_clock(period.get("start"))
    end = parse_clock(period.get("end"), end=True)
    days = normalize_days(period.get("days"))
    if name not in PERIOD_TYPES or start is None or end is None or days is None:
        return None
    rates: dict[str, float | None] = {}
    for key in ("import_rate", "export_rate"):
        raw = period.get(key)
        if raw is None:
            rates[key] = None
            continue
        try:
            rates[key] = float(raw)
        except (TypeError, ValueError):
            return None
    result: dict[str, Any] = {
        "name": name,
        "start": start,
        "end": end,
        "days": days,
        **rates,
    }
    label = period.get("label")
    if isinstance(label, str) and label.strip():
        result["label"] = label.strip()[:60]
    if "season" in period:
        result["season"] = str(period["season"])
    return result


def resolved_rates(
    period: dict[str, Any], standard_import: float, standard_export: float
) -> tuple[float, float]:
    import_rate = period.get("import_rate")
    export_rate = period.get("export_rate")
    return (
        standard_import if import_rate is None else float(import_rate),
        standard_export if export_rate is None else float(export_rate),
    )


# --- presets ----------------------------------------------------------------


def _first_word(value: Any) -> str:
    words = str(value or "").casefold().replace("-", " ").split()
    return words[0] if words else ""


def preset_options(retailer_label: str | None) -> list[tuple[str, str]]:
    """Australian presets for this retailer, then the generic starting points."""
    from .tariff_templates import TARIFF_TEMPLATES

    retailer_key = _first_word(retailer_label)
    matching: list[tuple[str, str]] = []
    generic: list[tuple[str, str]] = []
    for template_id, template in TARIFF_TEMPLATES.items():
        utility = template.get("utility")
        label = str(template.get("name") or template_id)
        if utility == "Generic":
            generic.append((template_id, label))
        elif retailer_key and _first_word(utility) == retailer_key:
            matching.append((template_id, label))
    return matching + generic


def template_to_draft(template: dict[str, Any], *, utility: str | None = None) -> dict[str, Any]:
    """Convert a Tesla-format preset into the editor's draft state."""
    seasons_raw = template.get("seasons") if isinstance(template.get("seasons"), dict) else {}
    energy = template.get("energy_charges") if isinstance(template.get("energy_charges"), dict) else {}
    sell_root = template.get("sell_tariff") if isinstance(template.get("sell_tariff"), dict) else {}
    sell = sell_root.get("energy_charges") if isinstance(sell_root.get("energy_charges"), dict) else {}

    def rate_map(section: Any) -> dict[str, float]:
        if isinstance(section, dict) and isinstance(section.get("rates"), dict):
            section = section["rates"]
        return {
            str(key): float(value)
            for key, value in (section or {}).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

    first_season = next(iter(seasons_raw), "All Year")
    first_import = rate_map(energy.get(first_season))
    first_export = rate_map(sell.get(first_season))
    standard_import = first_import.get("OFF_PEAK", first_import.get("ALL"))
    if standard_import is None:
        standard_import = min(first_import.values()) if first_import else 0.0
    standard_export = first_export.get("ALL", first_export.get("OFF_PEAK"))
    if standard_export is None:
        standard_export = min(first_export.values()) if first_export else 0.0

    seasons: list[dict[str, Any]] = []
    periods: list[dict[str, Any]] = []
    multiple = len(seasons_raw) > 1 or "All Year" not in seasons_raw
    for season_name, season in seasons_raw.items():
        if not isinstance(season, dict):
            continue
        seasons.append(
            {
                "name": str(season_name),
                "from_month": int(season.get("fromMonth", 1)),
                "to_month": int(season.get("toMonth", 12)),
            }
        )
        imports = rate_map(energy.get(season_name))
        exports = rate_map(sell.get(season_name))
        tou = season.get("tou_periods") if isinstance(season.get("tou_periods"), dict) else {}
        for internal_name, ranges in tou.items():
            entries = ranges.get("periods") if isinstance(ranges, dict) else ranges
            if not isinstance(entries, list):
                continue
            base_name = internal_name.rsplit("_", 1)[0] if internal_name not in PERIOD_TYPES else internal_name
            name = base_name if base_name in PERIOD_TYPES else "OFF_PEAK"
            import_rate = imports.get(internal_name)
            export_rate = exports.get(internal_name)
            if (import_rate is None or import_rate == standard_import) and (
                export_rate is None or export_rate == standard_export
            ):
                continue
            grouped: dict[tuple[int, int], list[int]] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                start = int(item.get("fromHour", 0)) * 60 + int(item.get("fromMinute", 0))
                end = int(item.get("toHour", 24)) * 60 + int(item.get("toMinute", 0))
                first, last = int(item.get("fromDayOfWeek", 0)), int(item.get("toDayOfWeek", 6))
                days = list(range(first, last + 1)) if first <= last else [*range(first, 7), *range(0, last + 1)]
                grouped.setdefault((start % 1440, end or 1440), []).extend(days)
            for (start, end), days in grouped.items():
                period = {
                    "name": name,
                    "start": parse_clock(start / 60),
                    "end": parse_clock(end / 60, end=True),
                    "days": normalize_days(days) or "all_days",
                    "import_rate": None if import_rate in (None, standard_import) else import_rate,
                    "export_rate": None if export_rate in (None, standard_export) else export_rate,
                }
                if multiple:
                    period["season"] = str(season_name)
                periods.append(period)
    quota = template.get("import_quota")
    supply = template.get("daily_supply_charge")
    return {
        "plan_name": str(template.get("name") or "Preset"),
        "utility": utility or str(template.get("utility") or "Custom Provider"),
        "standard_import_rate": float(standard_import),
        "standard_export_rate": float(standard_export),
        "daily_supply_charge": float(supply) if isinstance(supply, (int, float)) else None,
        "seasons": seasons or [{"name": "All Year", "from_month": 1, "to_month": 12}],
        "periods": periods,
        "import_quota": dict(quota) if isinstance(quota, dict) else None,
        "source": {"kind": "preset", "template_id": template.get("id") or template.get("template_id")},
        "warnings": [
            "Preset prices are a starting point. Replace them with the rates on your bill."
        ]
        if template.get("utility") == "Generic"
        else [],
    }


def apply_draft(flow: Any, draft: dict[str, Any]) -> None:
    """Load a draft into the flow attributes the custom tariff steps use."""
    flow._tariff_draft = draft
    flow._tariff_plan_name = draft.get("plan_name", "")
    flow._tariff_offpeak_rate = float(draft.get("standard_import_rate") or 0.0)
    flow._tariff_fit_rate = float(draft.get("standard_export_rate") or 0.0)
    flow._tariff_daily_supply_charge = draft.get("daily_supply_charge")
    flow._tariff_import_quota = draft.get("import_quota")
    flow._tariff_source = draft.get("source")
    flow._tariff_utility = draft.get("utility")
    flow._tariff_seasons = [dict(season) for season in draft.get("seasons") or []] or [
        {"name": "All Year", "from_month": 1, "to_month": 12}
    ]
    flow._tariff_periods = [
        period
        for period in (normalize_period(row) for row in draft.get("periods") or [])
        if period is not None
    ]


def clear_draft(flow: Any) -> None:
    """Forget any imported or preset plan (manual entry)."""
    for name in (
        "_tariff_draft",
        "_tariff_daily_supply_charge",
        "_tariff_import_quota",
        "_tariff_source",
        "_tariff_utility",
    ):
        setattr(flow, name, None)


def warnings_text(draft: dict[str, Any] | None) -> str:
    if not draft:
        return ""
    lines = [f"- {warning}" for warning in draft.get("warnings") or []]
    title = f"**{draft.get('plan_name') or 'Plan'}** loaded. Review every rate against your bill."
    return title + ("\n\nTo check:\n" + "\n".join(lines) if lines else "")


# --- allowance --------------------------------------------------------------


def quota_window(quota: Any) -> tuple[str, str] | None:
    if not isinstance(quota, dict):
        return None
    windows = quota.get("windows")
    if isinstance(windows, list) and windows and isinstance(windows[0], (list, tuple)) and len(windows[0]) == 2:
        return str(windows[0][0]), str(windows[0][1])
    start, end = quota.get("start_time"), quota.get("end_time")
    if isinstance(start, str) and isinstance(end, str):
        return start, end
    return None


def quota_effective_price(quota: Any) -> float | None:
    """Price (minor units) paid for allowance energy."""
    if not isinstance(quota, dict):
        return None
    try:
        base = float(quota.get("base_price_c_per_kwh"))
        bonus = float(quota.get("bonus_price_c_per_kwh", base))
    except (TypeError, ValueError):
        return None
    return max(0.0, base - min(base, bonus))


def build_import_quota(
    *,
    limit_kwh: Any,
    start: Any,
    end: Any,
    price_minor: Any,
    periods: list[dict[str, Any]],
    standard_import: float,
    name: str | None = None,
    timezone_token: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Build the custom tariff import quota from the allowance form."""
    try:
        limit = float(limit_kwh or 0)
        price = float(price_minor or 0)
    except (TypeError, ValueError):
        return None, "allowance_invalid"
    if limit <= 0:
        return None, None
    start_hours = parse_clock(start)
    end_hours = parse_clock(end, end=True)
    if start_hours is None or end_hours is None or start_hours == end_hours or price < 0:
        return None, "allowance_invalid"
    start_minute, end_minute = int(start_hours * 60), int(end_hours * 60)

    def overlaps(period: dict[str, Any]) -> bool:
        p_start, p_end = int(float(period["start"]) * 60), int(float(period["end"]) * 60)
        for slot in range(48):
            minute = slot * 30
            in_window = (
                start_minute <= minute < end_minute
                if start_minute < end_minute
                else minute >= start_minute or minute < end_minute
            )
            in_period = p_start <= minute < p_end if p_start < p_end else minute >= p_start or minute < p_end
            if in_window and in_period:
                return True
        return False

    # The allowance discounts whichever rate applies inside the window. Use
    # the lowest candidate (any season or day) so the discount never exceeds
    # the real benefit.
    underlying = {standard_import * 100} | {
        float(period["import_rate"]) * 100
        for period in periods
        if period.get("import_rate") is not None and overlaps(period)
    }
    base = min(underlying) if underlying else standard_import * 100
    discount = round(base - price, 6)
    if discount <= 0:
        return None, "allowance_not_cheaper"
    quota = {
        "enabled": True,
        "name": name or "Free import allowance",
        "windows": [[clock_text(start_hours), clock_text(end_hours)]],
        "daily_cap_kwh": limit,
        "base_price_c_per_kwh": round(base, 6),
        "bonus_price_c_per_kwh": discount,
    }
    if timezone_token:
        quota["timezone_token"] = timezone_token
    return quota, None


# --- period table -----------------------------------------------------------


def _rate_text(value: float | None, unit: str, standard: bool) -> str:
    if value is None:
        return "standard"
    text = f"{value * 100:.2f}{unit}"
    return text if not standard else f"{text} (standard)"


def render_period_table(
    periods: list[dict[str, Any]],
    *,
    standard_import: float,
    standard_export: float,
    unit: str,
    show_export: bool = True,
    show_season: bool = False,
    daily_supply: float | None = None,
    supply_unit: str = "",
    quota: dict[str, Any] | None = None,
) -> str:
    """Markdown table of the periods plus the standard rates and allowance."""
    lines = [
        f"Standard import {standard_import * 100:.2f}{unit}"
        + (f", standard export {standard_export * 100:.2f}{unit}" if show_export else "")
        + " outside the periods below."
    ]
    if daily_supply:
        lines[0] += f" Daily supply charge {daily_supply:.2f}{supply_unit}."
    if periods:
        header = ["#"] + (["Season"] if show_season else []) + ["Period", "Days", "Time", "Import"]
        if show_export:
            header.append("Export")
        lines += ["", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
        for index, period in enumerate(periods, 1):
            name = period.get("label") or PERIOD_TYPE_LABELS.get(period.get("name"), str(period.get("name")))
            row = [str(index)]
            if show_season:
                row.append(str(period.get("season", "All Year")))
            row += [
                str(name),
                days_text(period.get("days")),
                f"{clock_text(period.get('start'))}-{clock_text(period.get('end'))}",
                _rate_text(period.get("import_rate"), unit, False),
            ]
            if show_export:
                row.append(_rate_text(period.get("export_rate"), unit, False))
            lines.append("| " + " | ".join(row) + " |")
    else:
        lines += ["", "No periods yet: the standard rates apply all day."]
    window = quota_window(quota)
    if window and isinstance(quota, dict):
        price = quota_effective_price(quota)
        lines += [
            "",
            f"Free import allowance: first {float(quota.get('daily_cap_kwh', 0)):g} kWh "
            f"{window[0]}-{window[1]} at {(price or 0):.2f}{unit}, resets daily.",
        ]
    return "\n".join(lines)


def price_strip(
    periods: list[dict[str, Any]],
    *,
    standard_import: float,
    when: datetime,
    season: str | None = None,
) -> str:
    """A 48-character import price strip for one day (shortest period wins)."""
    weekday = (when.weekday() + 1) % 7
    yesterday = (weekday - 1) % 7
    prices: list[float] = []
    for slot in range(48):
        minute = slot * 30
        best: tuple[int, float] | None = None
        for period in periods:
            if season is not None and period.get("season", season) != season:
                continue
            start, end = int(float(period["start"]) * 60), int(float(period["end"]) * 60)
            days = day_numbers(period.get("days"))
            if start < end:
                active = weekday in days and start <= minute < end
            else:
                active = (weekday in days and minute >= start) or (yesterday in days and minute < end)
            if not active:
                continue
            duration = (end - start) % 1440 or 1440
            rate = standard_import if period.get("import_rate") is None else float(period["import_rate"])
            if best is None or duration < best[0]:
                best = (duration, rate)
        prices.append(standard_import if best is None else best[1])
    low, high = min(prices), max(prices)
    if high == low:
        return PRICE_STRIP_LEVELS[0] * 48
    scale = len(PRICE_STRIP_LEVELS) - 1
    return "".join(
        PRICE_STRIP_LEVELS[round((price - low) / (high - low) * scale)] for price in prices
    )


def render_period_overview(
    periods: list[dict[str, Any]],
    seasons: list[dict[str, Any]] | None,
    *,
    standard_import: float,
    standard_export: float,
    unit: str,
    show_export: bool = True,
    daily_supply: float | None = None,
    supply_unit: str = "",
    quota: dict[str, Any] | None = None,
    when: datetime | None = None,
) -> str:
    """Table plus today's 24-hour import price strip."""
    named = [season for season in seasons or [] if season.get("name")]
    show_season = len(named) > 1 or (len(named) == 1 and named[0]["name"] != "All Year")
    text = render_period_table(
        periods,
        standard_import=standard_import,
        standard_export=standard_export,
        unit=unit,
        show_export=show_export,
        show_season=show_season,
        daily_supply=daily_supply,
        supply_unit=supply_unit,
        quota=quota,
    )
    if when is not None and periods:
        season_name = None
        if show_season:
            from .tariff_time import find_season_for_month

            season_name = find_season_for_month(
                {
                    str(season["name"]): {
                        "fromMonth": season.get("from_month", 1),
                        "toMonth": season.get("to_month", 12),
                    }
                    for season in named
                },
                when.month,
            )
        strip = price_strip(periods, standard_import=standard_import, when=when, season=season_name)
        text += f"\n\nToday's import price, 00:00 to 24:00:\n\n`{strip}`"
    return text
