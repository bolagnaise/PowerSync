"""Regression tests for Tesla Fleet-to-BLE bridge identity mapping."""

from __future__ import annotations

import sys
import types
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
)


VIN_A = "5YJ3E1EA7NF0000A1"
VIN_B = "5YJ3E1EA7NF0000B2"


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
