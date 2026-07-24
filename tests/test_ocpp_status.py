"""Tests for shared OCPP status normalization helpers."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations

from power_sync.automations.ocpp_status import (  # noqa: E402
    claimed_hacs_ocpp_prefixes,
    extract_hacs_ocpp_prefix,
    is_hacs_ocpp_energy_entity,
    is_hacs_ocpp_power_entity,
    is_hacs_ocpp_status_entity,
    is_ocpp_charging,
    is_ocpp_vehicle_present,
    normalize_ocpp_status,
    should_end_ocpp_session,
    split_hacs_ocpp_connector_prefix,
)


def test_extracts_status_connector_prefix_before_status_suffix():
    assert extract_hacs_ocpp_prefix("sensor.evse_1_status_connector") == "evse_1"
    assert extract_hacs_ocpp_prefix("sensor.evse_1_status") == "evse_1"
    assert extract_hacs_ocpp_prefix("switch.evse_1_charge_control") == "evse_1"


def test_extracts_upstream_hacs_ocpp_measurand_prefixes():
    assert extract_hacs_ocpp_prefix("sensor.evse_1_power_active_import") == "evse_1"
    assert extract_hacs_ocpp_prefix("sensor.evse_1_energy_active_import_register") == "evse_1"
    assert (
        extract_hacs_ocpp_prefix("sensor.evse_1_connector_2_status_connector")
        == "evse_1_connector_2"
    )


def test_generic_entity_claim_suppresses_only_its_hacs_ocpp_prefix():
    entries = [
        SimpleNamespace(
            entity_id="switch.charger_charge_control",
            platform="ocpp",
        ),
        SimpleNamespace(
            entity_id="sensor.charger_power_active_import",
            platform="ocpp",
        ),
        SimpleNamespace(
            entity_id="sensor.second_power_active_import",
            platform="ocpp",
        ),
    ]

    assert claimed_hacs_ocpp_prefixes(
        ["switch.charger_charge_control"],
        entries,
    ) == {"charger"}


def test_non_ocpp_generic_entity_does_not_claim_matching_ocpp_prefix():
    entries = [
        SimpleNamespace(
            entity_id="switch.charger_charge_control",
            platform="template",
        ),
        SimpleNamespace(
            entity_id="sensor.charger_power_active_import",
            platform="ocpp",
        ),
    ]

    assert claimed_hacs_ocpp_prefixes(
        ["switch.charger_charge_control"],
        entries,
    ) == set()


def test_generic_connector_claim_does_not_hide_sibling_connector_prefix():
    entries = [
        SimpleNamespace(
            entity_id="sensor.evse_1_connector_1_status_connector",
            platform="ocpp",
        ),
        SimpleNamespace(
            entity_id="sensor.evse_1_connector_2_status_connector",
            platform="ocpp",
        ),
    ]

    assert claimed_hacs_ocpp_prefixes(
        ["sensor.evse_1_connector_2_status_connector"],
        entries,
    ) == {"evse_1_connector_2"}


def test_splits_multi_connector_prefix_for_hacs_api_calls():
    assert split_hacs_ocpp_connector_prefix("evse_1") == ("evse_1", None)
    assert split_hacs_ocpp_connector_prefix("evse_1_connector_2") == ("evse_1", 2)


def test_classifies_hacs_ocpp_entity_types():
    assert is_hacs_ocpp_status_entity("sensor.evse_1_status_connector") is True
    assert is_hacs_ocpp_status_entity("switch.evse_1_charge_control") is False
    assert is_hacs_ocpp_power_entity("sensor.evse_1_power_active_import") is True
    assert is_hacs_ocpp_power_entity("sensor.evse_1_current_power") is True
    assert is_hacs_ocpp_power_entity("sensor.evse_1_power_offered") is False
    assert is_hacs_ocpp_energy_entity("sensor.evse_1_energy_session") is True


def test_power_offered_remains_discoverable_without_becoming_delivered_power():
    assert extract_hacs_ocpp_prefix("sensor.evse_1_power_offered") == "evse_1"


def test_status_normalization_accepts_hacs_variants():
    assert normalize_ocpp_status("Suspended_EVSE") == "suspendedevse"
    assert normalize_ocpp_status("suspended-ev") == "suspendedev"


def test_vehicle_present_and_charging_use_status_and_power():
    assert is_ocpp_vehicle_present("Preparing") is True
    assert is_ocpp_vehicle_present("available") is False
    assert is_ocpp_vehicle_present("available", power_w=200) is True
    assert is_ocpp_charging("available", power_w=200) is True


def test_finishing_without_power_ends_session():
    assert should_end_ocpp_session("finishing", 0, has_session=True) is True
    assert should_end_ocpp_session("finishing", 120, has_session=True) is False
    assert should_end_ocpp_session("charging", 0, has_session=True) is False
    assert should_end_ocpp_session("available", 0, has_session=True) is True
    assert should_end_ocpp_session("available", 0, has_session=False) is False
