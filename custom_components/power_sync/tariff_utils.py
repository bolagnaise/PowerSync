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
from datetime import datetime, timedelta, timezone

_LOGGER = logging.getLogger(__name__)


@contextmanager
def _suppress_stdout():
    """Silence print() statements in the aemo_to_tariff library."""
    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        yield
    finally:
        sys.stdout = old_stdout


def get_network_tariff_rate(
    dt: datetime,
    network: str,
    tariff_code: str,
) -> float | None:
    """Return the network tariff component in c/kWh for a given time.

    Calls spot_to_tariff with rrp=0 so the result is *only* the network
    charge (no wholesale component).  Loss factors are set to 1.0 because
    the Flow Power formula applies its own GST multiplier.

    Args:
        dt: The timestamp to look up (timezone-aware preferred).
        network: aemo_to_tariff network parameter (e.g. "sapn", "victoria").
        tariff_code: Tariff code (e.g. "RESELE", "6900").

    Returns:
        Network tariff rate in c/kWh, or None on error.
    """
    try:
        from aemo_to_tariff import spot_to_tariff

        with _suppress_stdout():
            rate = spot_to_tariff(
                interval_time=dt,
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
        from aemo_to_tariff import spot_to_tariff

        now = datetime.now(tz=timezone(timedelta(hours=10)))  # AEST
        base_date = now.replace(hour=0, minute=0, second=0, microsecond=0)

        total = 0.0
        count = 0
        for slot in range(48):
            slot_time = base_date + timedelta(minutes=slot * 30)
            with _suppress_stdout():
                rate = spot_to_tariff(
                    interval_time=slot_time,
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


def get_tariff_codes_for_network(network_display: str) -> list[str]:
    """Return available tariff codes for a DNSP display name.

    Imports the appropriate aemo_to_tariff module and reads its ``tariffs``
    dict to discover valid tariff codes.

    Args:
        network_display: Display name (e.g. "SAPN", "Energex").

    Returns:
        List of tariff code strings, or empty list on error.
    """
    from .const import NETWORK_MODULE_NAME

    module_name = NETWORK_MODULE_NAME.get(network_display)
    if not module_name:
        _LOGGER.warning("No module mapping for network: %s", network_display)
        return []

    try:
        mod = importlib.import_module(f"aemo_to_tariff.{module_name}")
        tariffs = getattr(mod, "tariffs", {})
        return list(tariffs.keys())
    except Exception as err:
        _LOGGER.warning(
            "Failed to load tariff codes for %s (module=%s): %s",
            network_display, module_name, err,
        )
        return []


def get_networks_for_region(region: str) -> list[str]:
    """Return DNSP display names available in a NEM region.

    Args:
        region: NEM region code (e.g. "SA1", "NSW1").

    Returns:
        List of display name strings.
    """
    from .const import REGION_NETWORKS

    return REGION_NETWORKS.get(region, [])
