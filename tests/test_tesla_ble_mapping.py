"""Regression tests for Tesla Fleet-to-BLE bridge identity mapping."""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
package = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
package.__path__ = [str(ROOT)]

from power_sync.tesla_ble_mapping import (  # noqa: E402
    TeslaBleMappingError,
    ble_prefix_vehicle_pairs,
    canonical_tesla_vehicle_id,
    coalesce_paired_vehicle_configs,
    parse_tesla_ble_vehicle_mapping,
    resolve_ble_prefixes,
    vehicle_ble_prefix,
)
from power_sync.tesla_ble import (  # noqa: E402
    get_tesla_ble_battery_state,
    get_tesla_ble_charge_current_state,
    get_tesla_ble_charge_power_state,
    get_tesla_ble_charging_state,
    get_tesla_ble_plug_state,
    get_tesla_ble_status_state,
)


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


@pytest.mark.parametrize("node,connection,expected", [
    ("on", "off", "off"), ("on", "unknown", "unknown"),
    ("off", "on", "off"), ("on", "on", "on"),
])
def test_vehicle_connectivity_requires_node_and_ble_when_both_exist(node, connection, expected):
    hass = _hass_states(
        binary_sensor_home_ble_status=node,
        binary_sensor_home_ble_ble_status=connection,
    )
    assert get_tesla_ble_status_state(hass, "home_ble").state == expected


def _both_config(prefixes: str, mapping: str = "") -> dict[str, str]:
    return {
        "ev_provider": "both",
        "tesla_ble_entity_prefix": prefixes,
        "tesla_ble_vehicle_mapping": mapping,
    }


def _hass(*entity_ids: str) -> SimpleNamespace:
    states = {
        entity_id: SimpleNamespace(entity_id=entity_id, state="on")
        for entity_id in entity_ids
    }
    return SimpleNamespace(
        states=SimpleNamespace(
            get=states.get,
            async_all=lambda: list(states.values()),
        )
    )


def _hass_states(**states: str) -> SimpleNamespace:
    return SimpleNamespace(
        states=SimpleNamespace(
            get=lambda entity_id: (
                SimpleNamespace(
                    entity_id=entity_id,
                    state=states[entity_id.replace(".", "_")],
                )
                if entity_id.replace(".", "_") in states
                else None
            ),
        ),
    )


def test_current_yoziru_entities_fallback_for_ble_telemetry():
    hass = _hass_states(
        sensor_my_model_y_battery="67",
        sensor_my_model_y_charging="Stopped",
        sensor_my_model_y_charger_power="0",
        binary_sensor_my_model_y_charger="on",
    )

    assert get_tesla_ble_battery_state(hass, "my_model_y").state == "67"
    assert get_tesla_ble_charging_state(hass, "my_model_y").state == "Stopped"
    assert get_tesla_ble_charge_power_state(hass, "my_model_y").state == "0"
    assert get_tesla_ble_plug_state(hass, "my_model_y").state == "on"


def test_current_yoziru_current_falls_back_from_unavailable_legacy_entity():
    hass = _hass_states(
        sensor_my_model_y_charge_current="unavailable",
        sensor_my_model_y_charger_current="10",
    )

    assert get_tesla_ble_charge_current_state(hass, "my_model_y").state == "10"


def test_current_yoziru_entities_replace_unavailable_legacy_telemetry():
    hass = _hass_states(
        sensor_my_model_y_charge_level="unavailable",
        sensor_my_model_y_battery="67",
    )

    assert get_tesla_ble_battery_state(hass, "my_model_y").state == "67"


def test_charge_power_prefers_the_newest_finite_compatibility_reading():
    older = SimpleNamespace(
        state="2.0",
        last_updated=datetime(2026, 9, 5, 4, 0),
    )
    newer = SimpleNamespace(
        state="2.3",
        last_updated=datetime(2026, 9, 5, 4, 1),
    )
    hass = SimpleNamespace(
        states=SimpleNamespace(
            get={
                "sensor.my_model_y_charge_power": older,
                "sensor.my_model_y_charger_power": newer,
            }.get,
        )
    )

    assert get_tesla_ble_charge_power_state(hass, "my_model_y") is newer


def test_mapping_accepts_comma_and_newline_separators():
    assert parse_tesla_ble_vehicle_mapping(
        f"{VIN_A.lower()}=bridge_alpha,\n{VIN_B}=bridge_beta"
    ) == {
        VIN_A: "bridge_alpha",
        VIN_B: "bridge_beta",
    }


@pytest.mark.parametrize(
    "mapping",
    (
        "not-a-vin=bridge_alpha",
        f"{VIN_A}=Bridge-Alpha",
        f"{VIN_A}=bridge_alpha,{VIN_A}=bridge_beta",
        f"{VIN_A}=bridge_alpha,{VIN_B}=bridge_alpha",
        f"{VIN_A}:bridge_alpha",
    ),
)
def test_mapping_rejects_malformed_or_ambiguous_entries(mapping: str):
    with pytest.raises(TeslaBleMappingError):
        parse_tesla_ble_vehicle_mapping(mapping)


def test_explicit_mapping_does_not_depend_on_prefix_or_vin_order():
    config = _both_config(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_beta,{VIN_B}=bridge_alpha",
    )

    assert vehicle_ble_prefix(config, VIN_A, [VIN_B, VIN_A]) == "bridge_beta"
    assert vehicle_ble_prefix(config, VIN_B, [VIN_A, VIN_B]) == "bridge_alpha"
    assert ble_prefix_vehicle_pairs(config, [VIN_B, VIN_A]) == {
        "bridge_alpha": VIN_B,
        "bridge_beta": VIN_A,
    }


def test_unmapped_multi_vehicle_configuration_fails_closed():
    config = _both_config("bridge_alpha,bridge_beta")

    assert vehicle_ble_prefix(config, VIN_A, [VIN_A, VIN_B]) is None
    assert vehicle_ble_prefix(config, VIN_B, [VIN_A, VIN_B]) is None


def test_single_vehicle_single_bridge_is_inferred_safely():
    config = _both_config("bridge_alpha")

    assert vehicle_ble_prefix(config, VIN_A, [VIN_A]) == "bridge_alpha"


def test_ble_only_alias_never_borrows_lingering_fleet_identity():
    config = {
        "ev_provider": "tesla_ble",
        "tesla_ble_entity_prefix": "tesla_yf88,tesla_flinn",
        "tesla_ble_vehicle_mapping": f"{VIN_A}=tesla_yf88,{VIN_B}=tesla_flinn",
    }

    assert canonical_tesla_vehicle_id(
        config,
        "ble_tesla_yf88",
        [VIN_A, VIN_B],
    ) == "ble_tesla_yf88"
    assert canonical_tesla_vehicle_id(
        config,
        "ble_tesla_flinn",
        [VIN_A, VIN_B],
    ) == "ble_tesla_flinn"


def test_ble_only_multi_bridge_vin_command_fails_closed():
    config = {
        "ev_provider": "tesla_ble",
        "tesla_ble_entity_prefix": "tesla_yf88,tesla_flinn",
    }

    assert vehicle_ble_prefix(config, VIN_A) is None
    assert vehicle_ble_prefix(
        config,
        VIN_A,
        resolved_prefixes=["tesla_yf88", "tesla_flinn"],
    ) is None


def test_ble_only_single_bridge_vin_compatibility_remains_unambiguous():
    config = {
        "ev_provider": "tesla_ble",
        "tesla_ble_entity_prefix": "tesla_yf88",
    }

    assert vehicle_ble_prefix(config, VIN_A) == "tesla_yf88"


def test_mapping_to_an_unconfigured_bridge_fails_closed():
    config = _both_config("bridge_alpha", f"{VIN_A}=bridge_beta")

    assert vehicle_ble_prefix(config, VIN_A, [VIN_A]) is None


def test_single_vehicle_inference_uses_unambiguous_resolved_prefix():
    config = _both_config("tesla_ble")
    resolved = resolve_ble_prefixes(
        _hass("sensor.garage_ble_charging_state"),
        config,
    )

    assert resolved == ["garage_ble"]
    assert vehicle_ble_prefix(config, VIN_A, [VIN_A], resolved) == "garage_ble"
    assert ble_prefix_vehicle_pairs(config, [VIN_A], resolved) == {
        "garage_ble": VIN_A,
    }


def test_empty_prefix_falls_back_to_default_bridge():
    assert resolve_ble_prefixes(_hass(), _both_config("")) == ["tesla_ble"]


def test_ambiguous_autodetection_keeps_configured_prefix():
    config = _both_config("tesla_ble")

    assert resolve_ble_prefixes(
        _hass(
            "sensor.garage_ble_charging_state",
            "sensor.driveway_ble_charging_state",
        ),
        config,
    ) == ["tesla_ble"]


def test_paired_ble_alias_resolves_to_canonical_vin():
    config = _both_config(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_alpha,{VIN_B}=bridge_beta",
    )

    assert canonical_tesla_vehicle_id(config, "ble_bridge_alpha") == VIN_A
    assert canonical_tesla_vehicle_id(config, "ble_bridge_beta") == VIN_B


def test_standalone_ble_alias_remains_independent():
    config = _both_config("bridge_alpha,standalone_bridge", f"{VIN_A}=bridge_alpha")

    assert (
        canonical_tesla_vehicle_id(
            config,
            "ble_standalone_bridge",
            [VIN_A],
        )
        == "ble_standalone_bridge"
    )


def test_vehicle_configs_drop_paired_aliases_and_preserve_vin_settings():
    config = _both_config(
        "bridge_alpha,bridge_beta",
        f"{VIN_A}=bridge_alpha,{VIN_B}=bridge_beta",
    )
    configs = [
        {
            "vehicle_id": "ble_bridge_alpha",
            "display_name": "Tesla BLE (bridge_alpha)",
            "priority": 3,
            "max_amps": 32,
            "solar_charging_enabled": True,
        },
        {
            "vehicle_id": VIN_A,
            "display_name": "Primary EV",
            "priority": 1,
            "max_amps": 10,
            "solar_charging_enabled": False,
        },
        {
            "vehicle_id": VIN_B,
            "display_name": "Secondary EV",
            "priority": 2,
            "max_amps": 16,
            "solar_charging_enabled": True,
        },
        {
            "vehicle_id": "ble_bridge_beta",
            "display_name": "Tesla BLE (bridge_beta)",
            "priority": 4,
            "max_amps": 32,
            "solar_charging_enabled": True,
        },
    ]

    assert coalesce_paired_vehicle_configs(config, configs) == [
        configs[1],
        configs[2],
    ]


def test_alias_only_config_migrates_to_explicit_vin():
    config = _both_config("bridge_alpha", f"{VIN_A}=bridge_alpha")

    assert coalesce_paired_vehicle_configs(
        config,
        [{"vehicle_id": "ble_bridge_alpha", "priority": 1}],
    ) == [{"vehicle_id": VIN_A, "priority": 1}]


@pytest.mark.parametrize("power_suffix", ["charge_power", "charger_power"])
def test_ble_power_receipt_uses_timestamp_value_not_ha_report_time(power_suffix):
    from datetime import timedelta, timezone
    from power_sync.tesla_ble import get_tesla_ble_observed_at

    now = datetime.now(timezone.utc)
    received = now - timedelta(seconds=5)
    power = SimpleNamespace(entity_id=f"sensor.car_{power_suffix}", state="7.0",
                            last_reported=now - timedelta(minutes=5))
    receipt = SimpleNamespace(state=received.isoformat(), last_reported=now)
    hass = SimpleNamespace(states=SimpleNamespace(get={
        power.entity_id: power, power.entity_id + "_received": receipt,
    }.get))
    assert get_tesla_ble_observed_at(hass, power) == received


@pytest.mark.parametrize("value", ["unknown", "unavailable", "invalid", "2026-10-29T10:00:00"])
def test_invalid_present_ble_receipt_never_falls_back_to_ha_time(value):
    from datetime import timezone
    from power_sync.tesla_ble import get_tesla_ble_observed_at

    power = SimpleNamespace(entity_id="sensor.car_charge_power", state="7.0",
                            last_reported=datetime.now(timezone.utc))
    hass = SimpleNamespace(states=SimpleNamespace(get={
        power.entity_id + "_received": SimpleNamespace(state=value),
    }.get))
    assert get_tesla_ble_observed_at(hass, power) is None


def test_ble_alias_selection_uses_each_alias_own_receipt():
    from datetime import timedelta, timezone

    now = datetime.now(timezone.utc)
    older = SimpleNamespace(entity_id="sensor.car_charge_power", state="7.0", last_reported=now)
    newer = SimpleNamespace(entity_id="sensor.car_charger_power", state="3.0",
                            last_reported=now - timedelta(minutes=10))
    hass = SimpleNamespace(states=SimpleNamespace(get={
        older.entity_id: older, newer.entity_id: newer,
        older.entity_id + "_received": SimpleNamespace(state=(now - timedelta(minutes=5)).isoformat()),
        newer.entity_id + "_received": SimpleNamespace(state=(now - timedelta(seconds=5)).isoformat()),
    }.get))
    assert get_tesla_ble_charge_power_state(hass, "car") is newer


def test_ble_without_receipt_keeps_legacy_observation_timestamp():
    from datetime import timezone
    from power_sync.tesla_ble import get_tesla_ble_observed_at

    now = datetime.now(timezone.utc)
    power = SimpleNamespace(entity_id="sensor.car_charge_power", state="7.0", last_reported=now)
    hass = SimpleNamespace(states=SimpleNamespace(get=lambda _: None))
    assert get_tesla_ble_observed_at(hass, power) == now


@pytest.mark.parametrize("native,reported,valid", [(2, "2.0", True), (0, "0", True),
    (0, "2", False), (2, "0", False), (True, "1", False),
    ("Charging", "Charging", True), ("Stopped", "Charging", False)])
def test_paired_native_sample_cannot_refresh_an_older_value(native, reported, valid):
    from datetime import timedelta, timezone
    import json
    from power_sync.tesla_ble import get_tesla_ble_observed_at
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(seconds=5)
    state = SimpleNamespace(entity_id="sensor.car_charge_power", state=reported,
                            last_reported=now)
    states = {state.entity_id: state,
        state.entity_id + "_sample": SimpleNamespace(state=json.dumps({"v": 1, "t": stamp.isoformat(), "x": native})),
        state.entity_id + "_received": SimpleNamespace(state=now.isoformat())}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    assert get_tesla_ble_observed_at(hass, state) == (stamp if valid else None)


@pytest.mark.parametrize("packet", ['unavailable', '{}', '{"v":true}',
    '{"v":1,"x":2,"t":"2026-11-08T00:00:00"}',
    '{"v":1,"x":2,"t":"2999-01-01T00:00:00Z"}',
    '{"v":1,"x":NaN,"t":"2026-11-08T00:00:00Z"}'])
def test_invalid_paired_sample_never_falls_back_to_ha_or_receipt(packet):
    from datetime import timezone
    from power_sync.tesla_ble import get_tesla_ble_observed_at
    now = datetime.now(timezone.utc)
    power = SimpleNamespace(entity_id="sensor.car_charge_power", state="2", last_reported=now)
    states = {power.entity_id + "_sample": SimpleNamespace(state=packet),
              power.entity_id + "_received": SimpleNamespace(state=now.isoformat())}
    assert get_tesla_ble_observed_at(SimpleNamespace(states=SimpleNamespace(get=states.get)), power) is None


class _FakeRegistry:
    """Minimal HA entity registry: entities indexed by device."""

    def __init__(self, entries):
        self._entries = {entry.entity_id: entry for entry in entries}
        registry = self

        class _Items(dict):
            def get_entries_for_device_id(self, device_id, include_disabled_entities=False):
                return [e for e in registry._entries.values()
                        if e.device_id == device_id
                        and (include_disabled_entities or e.disabled_by is None)]

        self.entities = _Items(self._entries)

    def async_get(self, entity_id):
        return self._entries.get(entity_id)


def _reg(entity_id, name, device="bridge-1", disabled_by=None):
    return SimpleNamespace(entity_id=entity_id, original_name=name, name=None,
                           device_id=device, disabled_by=disabled_by)


def _renamed_bridge_hass(monkeypatch, *, extra_entries=(), extra_states=None, packet_value=0):
    """Ticket #158 layout: raw entities predate a device rename, companions do not."""
    import json
    from datetime import timedelta, timezone
    import power_sync.tesla_ble as tesla_ble

    now = datetime.now(timezone.utc)
    stamp = now - timedelta(seconds=5)
    raw = SimpleNamespace(entity_id="sensor.teslablefbd_charge_power", state="0",
                          last_reported=now - timedelta(minutes=30))
    sample_id = "sensor.garage_teslablefbd_charge_power_sample"
    states = {
        raw.entity_id: raw,
        sample_id: SimpleNamespace(entity_id=sample_id, state=json.dumps(
            {"v": 1, "t": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "x": packet_value})),
        **(extra_states or {}),
    }
    registry = _FakeRegistry([
        _reg(raw.entity_id, "Charge power"),
        _reg(sample_id, "Charge power sample"),
        _reg("sensor.garage_teslablefbd_charge_level_sample", "Charge level sample"),
        *extra_entries,
    ])
    monkeypatch.setattr(tesla_ble, "_entity_registry", lambda _hass: registry)
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    return hass, raw, stamp.replace(microsecond=0)


def test_renamed_device_sample_companion_is_found_through_registry(monkeypatch):
    from power_sync.tesla_ble import get_tesla_ble_observed_at

    hass, raw, stamp = _renamed_bridge_hass(monkeypatch)
    # Previously this fell back to the 30-minute-old HA timestamp.
    assert get_tesla_ble_observed_at(hass, raw) == stamp


def test_renamed_device_sample_still_fails_closed_on_value_mismatch(monkeypatch):
    from power_sync.tesla_ble import get_tesla_ble_observed_at

    hass, raw, _ = _renamed_bridge_hass(monkeypatch, packet_value=7)
    assert get_tesla_ble_observed_at(hass, raw) is None


def test_companion_match_by_original_name_when_raw_entity_was_renamed(monkeypatch):
    import json
    from datetime import timedelta, timezone
    import power_sync.tesla_ble as tesla_ble
    from power_sync.tesla_ble import get_tesla_ble_companion_state

    now = datetime.now(timezone.utc)
    raw_id = "sensor.garage_car_charging_state"
    sample_id = "sensor.car_charging_state_sample"
    sample = SimpleNamespace(entity_id=sample_id, state=json.dumps(
        {"v": 1, "t": (now - timedelta(seconds=2)).isoformat(), "x": "Stopped"}))
    registry = _FakeRegistry([_reg(raw_id, "Charging state"), _reg(sample_id, "Charging state sample")])
    monkeypatch.setattr(tesla_ble, "_entity_registry", lambda _hass: registry)
    hass = SimpleNamespace(states=SimpleNamespace(get={sample_id: sample}.get))
    assert get_tesla_ble_companion_state(hass, raw_id, "_sample") is sample


@pytest.mark.parametrize("extra", [
    # A second candidate on the same device is ambiguous.
    [_reg("sensor.old_teslablefbd_charge_power_sample", "Charge power sample")],
])
def test_ambiguous_registry_companions_fail_closed_to_legacy(monkeypatch, extra):
    from power_sync.tesla_ble import get_tesla_ble_companion_state

    hass, raw, _ = _renamed_bridge_hass(monkeypatch, extra_entries=extra)
    assert get_tesla_ble_companion_state(hass, raw.entity_id, "_sample") is None


def test_registry_companion_never_crosses_devices_or_uses_disabled_entries(monkeypatch):
    import power_sync.tesla_ble as tesla_ble
    from power_sync.tesla_ble import get_tesla_ble_companion_state

    raw_id = "sensor.car_charge_power"
    registry = _FakeRegistry([
        _reg(raw_id, "Charge power"),
        _reg("sensor.other_car_charge_power_sample", "Charge power sample", device="bridge-2"),
        _reg("sensor.garage_car_charge_power_sample", "Charge power sample", disabled_by="user"),
    ])
    monkeypatch.setattr(tesla_ble, "_entity_registry", lambda _hass: registry)
    states = {"sensor.other_car_charge_power_sample": SimpleNamespace(state="{}"),
              "sensor.garage_car_charge_power_sample": SimpleNamespace(state="{}")}
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    assert get_tesla_ble_companion_state(hass, raw_id, "_sample") is None


def test_exact_companion_name_wins_over_registry(monkeypatch):
    import power_sync.tesla_ble as tesla_ble
    from power_sync.tesla_ble import get_tesla_ble_companion_state

    exact = SimpleNamespace(state="exact")
    monkeypatch.setattr(tesla_ble, "_entity_registry",
                        lambda _hass: (_ for _ in ()).throw(AssertionError("registry consulted")))
    hass = SimpleNamespace(states=SimpleNamespace(get={"sensor.car_charge_power_sample": exact}.get))
    assert get_tesla_ble_companion_state(hass, "sensor.car_charge_power", "_sample") is exact


def test_ble_recovery_status_reports_renamed_companion_as_paired(monkeypatch):
    from power_sync.tesla_ble_recovery_status import ble_recovery_status
    import power_sync.tesla_ble_mapping as mapping

    hass, raw, _ = _renamed_bridge_hass(monkeypatch)
    hass.data = {"power_sync": {"entry": {}}}
    monkeypatch.setattr(mapping, "resolve_ble_prefixes", lambda _h, _c: ["teslablefbd"])
    import power_sync.tesla_ble as tesla_ble
    monkeypatch.setattr(tesla_ble, "get_tesla_ble_charge_power_state", lambda _h, _p: raw)
    import power_sync.tesla_ble_stop_recovery as stop_recovery
    monkeypatch.setattr(stop_recovery, "_retained_root", lambda _runtime: {"vehicles": {}})
    monkeypatch.setattr(stop_recovery, "_live_refs", lambda _runtime: [])
    entry = SimpleNamespace(entry_id="entry", data={}, options={})
    result = ble_recovery_status(hass, entry)
    assert result["success"], result
    assert result["bridges"][0]["power_receipt"] == "paired_native_value"
    assert result["bridges"][0]["power_sample_fresh"] is True
