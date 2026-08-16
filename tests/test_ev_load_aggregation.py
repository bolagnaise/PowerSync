from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "power_sync_ev_load",
    Path(__file__).parents[1] / "custom_components/power_sync/ev_load.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

from power_sync_ev_load import (  # noqa: E402
    EvLoadObservation,
    EvLoadQuality,
    EvMeasurementKind,
    ObservedEvLoadSnapshot,
    aggregate_ev_load,
    meter_physical_load_key,
    normalize_power_kw,
    reconcile_ev_load_snapshot,
)


NOW = datetime(2026, 8, 14, 0, 20, tzinfo=timezone.utc)


def obs(key, source, power, *, seconds=0, kind=EvMeasurementKind.VEHICLE, active=True, v2x=False):
    return EvLoadObservation(
        physical_load_key=key,
        source_key=source,
        power_kw=power,
        observed_at=NOW - timedelta(seconds=seconds),
        active=active,
        measurement_kind=kind,
        supports_bidirectional_power=v2x,
    )


def test_duplicate_fleet_and_ble_select_one_physical_vehicle():
    result = aggregate_ev_load(
        [obs("vehicle:one", "fleet", 1.0), obs("vehicle:one", "ble", 2.0)],
        at=NOW,
    )
    assert result.power_kw in (1.0, 2.0)
    assert len(result.components) == 1


def test_direct_meter_outranks_vehicle_and_distinct_chargers_sum():
    result = aggregate_ev_load(
        [
            obs("vehicle:one", "fleet", 7.0),
            obs("vehicle:one", "wall_connector", 7.2, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("ocpp:cp-2:1", "ocpp", 11.0, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("zaptec:z-3", "zaptec", 3.6, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("sigenergy:evdc", "evdc", 4.0, kind=EvMeasurementKind.INTEGRATED_CHARGER),
            obs("solaredge:internal", "solaredge", 2.4, kind=EvMeasurementKind.INTEGRATED_CHARGER),
            obs("generic:entry", "generic", 1.2, kind=EvMeasurementKind.LOADPOINT_METER),
        ],
        at=NOW,
    )
    assert result.power_kw == pytest.approx(29.4)
    assert len(result.components) == 6


def test_two_wall_connectors_and_a_distinct_umc_sum():
    result = aggregate_ev_load(
        [
            obs("wall_connector:one", "wc1", 7.2, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("wall_connector:two", "wc2", 11.0, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("vehicle:umc", "ble", 2.0),
        ],
        at=NOW,
    )
    assert result.power_kw == pytest.approx(20.2)


def test_stale_primary_falls_back_to_fresh_vehicle_reading():
    result = aggregate_ev_load(
        [
            obs("vehicle:one", "meter", 7.2, seconds=100, kind=EvMeasurementKind.LOADPOINT_METER),
            obs("vehicle:one", "fleet", 6.8, seconds=5),
        ],
        at=NOW,
        max_age=timedelta(seconds=90),
    )
    assert result.power_kw == 6.8


def test_invalid_active_reading_marks_snapshot_incomplete():
    result = aggregate_ev_load([obs("ocpp:one", "ocpp", float("nan"))], at=NOW)
    assert result.quality == EvLoadQuality.INCOMPLETE
    assert result.unavailable_active_keys == ("ocpp:one",)


def test_fresh_idle_zero_is_complete():
    result = aggregate_ev_load([obs("generic:one", "generic", 0, active=False)], at=NOW)
    assert result.quality == EvLoadQuality.COMPLETE
    assert result.power_kw == 0


def test_unit_and_sign_normalization():
    assert normalize_power_kw(7200, "W") == 7.2
    assert normalize_power_kw(7.2, "kW") == 7.2
    assert normalize_power_kw(-3, "kW") is None
    assert normalize_power_kw(-3, "kW", supports_bidirectional_power=True) == -3
    assert normalize_power_kw(float("inf"), "kW") is None
    assert normalize_power_kw("bad", "kW") is None


@pytest.mark.parametrize(
    ("charger_type", "kwargs", "expected"),
    [
        ("ocpp", {"native_id": 3}, "ocpp:3:1"),
        ("zaptec", {"zaptec_id": "z-1"}, "zaptec:z-1"),
        ("sigenergy", {"sigenergy_type": "evdc"}, "sigenergy:evdc"),
        ("solaredge", {}, "solaredge:entry-1:internal"),
        ("generic", {}, "generic:sensor.charger_power"),
    ],
)
def test_native_and_entity_meter_keys_prevent_double_counting(
    charger_type,
    kwargs,
    expected,
):
    assert meter_physical_load_key(
        charger_type=charger_type,
        entity_id="sensor.charger_power",
        entry_id="entry-1",
        **kwargs,
    ) == expected


def test_future_observation_is_not_applied_backwards():
    future = EvLoadObservation("vehicle:one", "fleet", 7.0, NOW + timedelta(seconds=1), True)
    result = aggregate_ev_load([future], at=NOW)
    assert result.power_kw == 0
    assert result.quality == EvLoadQuality.INCOMPLETE


def test_direct_exact_key_replacement_preserves_distinct_v2x_power():
    snapshot = ObservedEvLoadSnapshot(
        power_kw=-2.0,
        components=(
            obs("vehicle:one", "cached", 1.0),
            obs("vehicle:v2x", "bidirectional", -3.0, v2x=True),
        ),
        observed_at=NOW,
        quality=EvLoadQuality.COMPLETE,
    )

    result = reconcile_ev_load_snapshot(
        snapshot,
        at=NOW,
        fallback_by_physical_key={"vehicle:one": 0.0},
        fallback_observed_at=NOW,
    )

    assert result.power_kw == -3.0
    assert result.quality == EvLoadQuality.COMPLETE
    assert {item.physical_load_key for item in result.components} == {
        "vehicle:one",
        "vehicle:v2x",
    }


def test_unrelated_direct_key_does_not_duplicate_complete_snapshot():
    snapshot = ObservedEvLoadSnapshot(
        power_kw=1.0,
        components=(obs("vehicle:one", "cached", 1.0),),
        observed_at=NOW,
        quality=EvLoadQuality.COMPLETE,
    )

    result = reconcile_ev_load_snapshot(
        snapshot,
        at=NOW,
        fallback_by_physical_key={"vehicle:two": 2.0},
        fallback_observed_at=NOW,
    )

    assert result.power_kw == 1.0
    assert tuple(item.physical_load_key for item in result.components) == (
        "vehicle:one",
    )


def test_missing_direct_timestamp_does_not_override_current_snapshot():
    snapshot = ObservedEvLoadSnapshot(
        power_kw=0.0,
        components=(obs("vehicle:one", "current", 0.0),),
        observed_at=NOW,
        quality=EvLoadQuality.COMPLETE,
    )

    result = reconcile_ev_load_snapshot(
        snapshot,
        at=NOW,
        fallback_power_kw=11.0,
        fallback_by_physical_key={"vehicle:one": 11.0},
    )

    assert result.power_kw == 0.0
    assert result.quality == EvLoadQuality.COMPLETE
    assert result.components == snapshot.components
