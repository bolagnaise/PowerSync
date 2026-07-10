"""Utility wrapper around the aemo_to_tariff library for Flow Power v2 tariff integration.

Provides functions to look up network tariff rates, compute daily averages,
and discover available tariff codes — all while suppressing the library's
internal print() statements.
"""
from __future__ import annotations

import importlib
import io
import logging
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)


def with_hysteresis(
    current: float,
    was_active: bool,
    *,
    enter_threshold: float,
    exit_threshold: float,
) -> bool:
    """Evaluate a boundary condition with hysteresis (HD-15 / HD-24).

    A single stateless `current >= threshold` (or `<`) comparison flaps
    active/inactive on every poll when ``current`` hovers right at the
    boundary. This adds a dead zone: once active, the condition only
    releases past ``exit_threshold`` rather than back at ``enter_threshold``.

    Whether "active" means "above" or "below" the nominal threshold is
    inferred from which of the two thresholds is larger:

    - ``enter_threshold >= exit_threshold``: active while high (e.g. a
      price spike). Enters at ``current >= enter_threshold``; once active,
      stays active until ``current`` drops below ``exit_threshold``.
    - ``enter_threshold < exit_threshold``: active while low (e.g. an
      uneconomic export price). Enters at ``current < enter_threshold``;
      once active, stays active until ``current`` rises to ``exit_threshold``
      or above.
    """
    active_when_high = enter_threshold >= exit_threshold
    boundary = exit_threshold if was_active else enter_threshold
    if active_when_high:
        return current >= boundary
    return current < boundary


@contextmanager
def _suppress_stdout():
    """Silence print() statements in the aemo_to_tariff library."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old_stdout


def _dispatch_interval_end(dt: datetime) -> datetime:
    """Return the dispatch interval end for a wall-clock timestamp."""
    interval_seconds = 5 * 60
    seconds_since_boundary = (dt.minute % 5) * 60 + dt.second
    if seconds_since_boundary == 0 and dt.microsecond == 0:
        return dt + timedelta(seconds=interval_seconds)
    return dt + timedelta(
        seconds=interval_seconds - seconds_since_boundary,
        microseconds=-dt.microsecond,
    )


def get_network_tariff_rate(
    dt: datetime,
    network: str,
    tariff_code: str,
) -> float | None:
    """Return the network tariff component in c/kWh for a given time.

    Calls spot_to_tariff with rrp=0 so the result is *only* the network
    charge (no wholesale component).  Loss factors are set to 1.0 because
    the Flow Power formula applies its own GST multiplier.

    aemo_to_tariff expects the NEM dispatch interval end. PowerSync passes
    wall-clock/start-of-interval timestamps, so look up the next 5-minute
    dispatch boundary to avoid publishing the previous tariff window.

    Args:
        dt: The timestamp to look up (timezone-aware preferred).
        network: aemo_to_tariff network parameter (e.g. "sapn", "victoria").
        tariff_code: Tariff code (e.g. "RESELE", "6900").

    Returns:
        Network tariff rate in c/kWh, or None on error.
    """
    try:
        from aemo_to_tariff import spot_to_tariff

        lookup_dt = _dispatch_interval_end(dt)
        with _suppress_stdout():
            rate = spot_to_tariff(
                interval_time=lookup_dt,
                network=network,
                tariff=tariff_code,
                rrp=0,
                dlf=1.0,
                mlf=1.0,
                market=1.0,
            )
        return float(rate)
    except Exception as err:
        _LOGGER.warning(
            "Failed to get network tariff rate for %s/%s at %s: %s",
            network, tariff_code, dt, err,
        )
        return None


def compute_avg_daily_tariff(
    network: str,
    tariff_code: str,
) -> float | None:
    """Compute the 24-hour average of the network tariff rate.

    Samples all 48 half-hour slots (using today's date) and averages them.
    This value is subtracted in the v2 PEA formula so that the network
    tariff component nets to zero over a full day.

    Args:
        network: aemo_to_tariff network parameter.
        tariff_code: Tariff code.

    Returns:
        Average daily tariff in c/kWh, or None on error.
    """
    try:
        from homeassistant.util import dt as dt_util
        from aemo_to_tariff import spot_to_tariff

        now = dt_util.now()  # Uses HA configured timezone
        base_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total = 0.0
        count = 0
        for slot in range(48):
            slot_time = base_date + timedelta(minutes=slot * 30)
            with _suppress_stdout():
                rate = spot_to_tariff(
                    interval_time=_dispatch_interval_end(slot_time),
                    network=network,
                    tariff=tariff_code,
                    rrp=0,
                    dlf=1.0,
                    mlf=1.0,
                    market=1.0,
                )
            total += float(rate)
            count += 1

        if count == 0:
            return None

        avg = round(total / count, 4)
        _LOGGER.debug(
            "Average daily tariff for %s/%s: %.4f c/kWh (%d slots)",
            network, tariff_code, avg, count,
        )
        return avg
    except Exception as err:
        _LOGGER.warning(
            "Failed to compute avg daily tariff for %s/%s: %s",
            network, tariff_code, err,
        )
        return None


def get_tariff_codes_for_network(network_display: str) -> dict[str, str]:
    """Return available tariff codes with names for a DNSP display name.

    Imports the appropriate aemo_to_tariff module and reads its ``tariffs``
    dict to discover valid tariff codes and their descriptive names.

    Args:
        network_display: Display name (e.g. "SAPN", "Energex").

    Returns:
        Dict of {tariff_code: display_name}, or empty dict on error.
    """
    from .const import NETWORK_MODULE_NAME

    module_name = NETWORK_MODULE_NAME.get(network_display)
    if not module_name:
        _LOGGER.warning("No module mapping for network: %s", network_display)
        return {}

    try:
        mod = importlib.import_module(f"aemo_to_tariff.{module_name}")
        get_tariffs = getattr(mod, "get_tariffs", None)
        tariffs = (
            get_tariffs() if callable(get_tariffs) else getattr(mod, "tariffs", {})
        )
        result = {}
        for code, data in tariffs.items():
            if isinstance(data, dict) and "name" in data:
                result[str(code)] = f"{code} — {data['name']}"
            else:
                result[str(code)] = str(code)
        return result
    except Exception as err:
        _LOGGER.warning(
            "Failed to load tariff codes for %s (module=%s): %s",
            network_display, module_name, err,
        )
        return {}


def get_networks_for_region(region: str) -> list[str]:
    """Return DNSP display names available in a NEM region.

    Args:
        region: NEM region code (e.g. "SA1", "NSW1").

    Returns:
        List of display name strings.
    """
    from .const import REGION_NETWORKS

    return REGION_NETWORKS.get(region, [])
