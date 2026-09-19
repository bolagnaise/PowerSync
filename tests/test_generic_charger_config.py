"""Regression tests for Generic Charger Solar Surplus configuration."""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

_ps = types.ModuleType("power_sync")
_ps.__path__ = [str(ROOT)]
sys.modules["power_sync"] = _ps

_automations = types.ModuleType("power_sync.automations")
_automations.__path__ = [str(ROOT / "automations")]
sys.modules["power_sync.automations"] = _automations


def _load_subject():
    sys.modules.pop("power_sync.automations.generic_charger_config", None)
    return importlib.import_module("power_sync.automations.generic_charger_config")


def test_backfill_adds_every_missing_generic_entity_from_entry_options():
    subject = _load_subject()
    profile = {"charger_type": "generic"}
    options = {
        "generic_charger_enabled": True,
        "generic_charger_switch_entity": "switch.ev_charger",
        "generic_charger_amps_entity": "number.ev_charger_amps",
        "generic_charger_status_entity": "sensor.ev_charger_status",
        "generic_charger_power_entity": "sensor.ev_charger_power",
    }

    assert subject.resolve_generic_charger_profile(profile, options) == "generic"
    assert profile == {
        "charger_type": "generic",
        "charger_switch_entity": "switch.ev_charger",
        "charger_amps_entity": "number.ev_charger_amps",
        "charger_status_entity": "sensor.ev_charger_status",
        "charger_power_entity": "sensor.ev_charger_power",
    }


def test_backfill_preserves_generic_profile_type_and_adds_missing_power_entity():
    subject = _load_subject()
    profile = {
        "vehicle_id": "generic_ev",
        "charger_type": "generic",
        "charger_switch_entity": "switch.ev_charger",
        "charger_amps_entity": "number.ev_charger_amps",
        "charger_status_entity": "sensor.ev_charger_status",
    }
    options = {
        "generic_charger_enabled": True,
        "generic_charger_switch_entity": "switch.ev_charger",
        "generic_charger_amps_entity": "number.ev_charger_amps",
        "generic_charger_status_entity": "sensor.ev_charger_status",
        "generic_charger_power_entity": "sensor.ev_charger_power",
    }

    charger_type = subject.resolve_generic_charger_profile(profile, options)

    assert charger_type == "generic"
    assert profile["charger_power_entity"] == "sensor.ev_charger_power"


def test_backfill_does_not_override_a_profile_specific_generic_power_entity():
    subject = _load_subject()
    profile = {
        "charger_type": "generic",
        "charger_power_entity": "sensor.profile_specific_power",
    }
    options = {
        "generic_charger_enabled": True,
        "generic_charger_power_entity": "sensor.global_power",
    }

    charger_type = subject.resolve_generic_charger_profile(profile, options)

    assert charger_type == "generic"
    assert profile["charger_power_entity"] == "sensor.profile_specific_power"


def test_disabled_generic_charger_does_not_inherit_stale_entry_entities():
    subject = _load_subject()
    profile = {"charger_type": "generic"}
    options = {
        "generic_charger_enabled": False,
        "generic_charger_power_entity": "sensor.stale_global_power",
    }

    charger_type = subject.resolve_generic_charger_profile(profile, options)

    assert charger_type == "generic"
    assert profile == {"charger_type": "generic"}


def test_disabled_generic_charger_does_not_turn_an_untyped_profile_generic():
    subject = _load_subject()
    profile = {}

    charger_type = subject.resolve_generic_charger_profile(
        profile,
        {"generic_charger_enabled": False},
    )

    assert charger_type is None
    assert profile == {}


def test_non_generic_profiles_do_not_inherit_generic_entities():
    subject = _load_subject()
    options = {
        "generic_charger_enabled": True,
        "generic_charger_power_entity": "sensor.generic_power",
    }

    for charger_type in ("ocpp", "tesla"):
        profile = {"charger_type": charger_type}

        assert subject.resolve_generic_charger_profile(profile, options) == charger_type
        assert profile == {"charger_type": charger_type}


def test_untyped_profile_remains_available_for_ocpp_fallback():
    subject = _load_subject()
    profile = {}

    assert subject.resolve_generic_charger_profile(
        profile,
        {"generic_charger_enabled": False, "ocpp_enabled": True},
    ) is None
    assert profile == {}
