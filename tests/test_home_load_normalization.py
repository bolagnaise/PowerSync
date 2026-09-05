from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "power_sync_ev_load_home",
    Path(__file__).parents[1] / "custom_components/power_sync/ev_load.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

from power_sync_ev_load_home import (  # noqa: E402
    EvLoadQuality,
    HomeLoadBasis,
    ObservedEvLoadSnapshot,
    normalize_energy_data,
    normalize_home_load,
)


NOW = datetime(2026, 8, 14, 0, 20, tzinfo=timezone.utc)


def ev(power, quality=EvLoadQuality.COMPLETE):
    return ObservedEvLoadSnapshot(power, (), NOW, quality)


def test_gross_powerwall_load_subtracts_w3_once():
    result = normalize_home_load(5.67, HomeLoadBasis.INCLUDES_EV, ev(1.0), at=NOW)
    assert result.non_ev_home_load_kw == 4.67


def test_already_normalized_load_is_not_subtracted_again():
    result = normalize_home_load(4.67, HomeLoadBasis.EXCLUDES_EV, ev(1.0), at=NOW)
    assert result.non_ev_home_load_kw == 4.67


def test_v2x_signed_power_reconstructs_household_demand():
    result = normalize_home_load(2.0, HomeLoadBasis.INCLUDES_EV, ev(-3.0), at=NOW)
    assert result.non_ev_home_load_kw == 5.0


def test_unknown_basis_fails_closed():
    result = normalize_home_load(5.0, HomeLoadBasis.UNKNOWN, ev(1.0), at=NOW)
    assert result.non_ev_home_load_kw is None
    assert result.normalization_quality == EvLoadQuality.INCOMPLETE


def test_incomplete_ev_coverage_does_not_publish_gross_as_home_load():
    result = normalize_home_load(
        5.0,
        HomeLoadBasis.INCLUDES_EV,
        ev(0.0, EvLoadQuality.INCOMPLETE),
        at=NOW,
    )
    assert result.non_ev_home_load_kw is None


def test_tesla_reconstructs_raw_before_using_complete_site_ev_total():
    result = normalize_energy_data(
        {"load_power": 5.67, "ev_power": 0.0},
        battery_system="tesla",
        ev_load=ev(1.0),
        at=NOW,
    )
    assert result["raw_home_load_power"] == 5.67
    assert result["load_power"] == 4.67
    assert result["site_load_power"] == 5.67


def test_tesla_embedded_ev_is_not_subtracted_twice():
    result = normalize_energy_data(
        {"load_power": 4.67, "ev_power": 1.0},
        battery_system="tesla",
        ev_load=ev(1.0),
        at=NOW,
    )
    assert result["load_power"] == 4.67


def test_sigenergy_reconstructs_raw_balance_before_normalizing():
    result = normalize_energy_data(
        {
            "solar_power": 3.0,
            "grid_power": 2.0,
            "battery_power": -1.0,
            "load_power": 3.0,
            "ev_power": 1.0,
        },
        battery_system="sigenergy",
        ev_load=ev(1.0),
        at=NOW,
    )
    assert result["raw_home_load_power"] == 4.0
    assert result["load_power"] == 3.0


def test_sigenergy_canonical_discharge_prevents_zero_home_load_clamp():
    """#399: native negative discharge is normalized before this balance."""
    result = normalize_energy_data(
        {
            "solar_power": 0.0,
            "grid_power": 0.712,
            "battery_power": 0.905,
            "load_power": 0.0,
        },
        battery_system="sigenergy",
        ev_load=ev(0.0),
        at=NOW,
    )

    assert result["raw_home_load_power"] == 1.617
    assert result["load_power"] == 1.617


def test_fresh_complete_snapshot_recovers_after_incomplete_cycle():
    incomplete = normalize_energy_data(
        {"load_power": 5.67, "ev_power": 0.0},
        battery_system="tesla",
        ev_load=ev(0.0, EvLoadQuality.INCOMPLETE),
        at=NOW,
    )
    assert incomplete["load_power"] is None
    assert incomplete["raw_home_load_power"] == 5.67
    assert incomplete["site_load_power"] == 5.67

    recovered = normalize_energy_data(
        incomplete,
        battery_system="tesla",
        ev_load=ev(1.0),
        at=NOW,
    )
    assert recovered["raw_home_load_power"] == 5.67
    assert recovered["load_power"] == 4.67


def test_ticket_371_double_counted_ev_would_clamp_home_load_to_zero():
    """A duplicated Wall Connector row hides the whole household load.

    The reporter's site: 1.7 kW solar, 15.0 kW import, 10.0 kW battery charge
    gives a 6.7 kW gross load with one 5.7 kW car.  Counting the BLE row and
    the Wall Connector row as two cars over-subtracts, and the ``max(0.0, …)``
    clamp turns that into a plausible-looking measured ``0 W``.
    """
    double_counted = normalize_home_load(
        6.7, HomeLoadBasis.INCLUDES_EV, ev(11.7), at=NOW
    )
    assert double_counted.non_ev_home_load_kw == 0.0

    coalesced = normalize_home_load(
        6.7, HomeLoadBasis.INCLUDES_EV, ev(5.7), at=NOW
    )
    assert coalesced.non_ev_home_load_kw == pytest.approx(1.0)
