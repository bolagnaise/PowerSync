"""Regression tests for SAPN/network-envelope Home Assistant wiring."""
from __future__ import annotations

import ast
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "power_sync"


def _source(name: str) -> str:
    return (INTEGRATION / name).read_text(encoding="utf-8")


def test_network_export_options_flow_exposes_fail_closed_active_gates() -> None:
    source = _source("config_flow.py")

    assert '"network_export"' in source
    assert "async_step_network_export" in source
    for error in (
        "network_export_fallback_required",
        "network_export_pcc_required",
        "network_export_der_attestation_required",
        "network_export_aggregate_required",
        "network_export_source_untrusted",
        "network_export_limit_invalid",
        "network_export_pcc_invalid",
    ):
        assert error in source


def test_network_envelope_api_is_get_only_and_lifecycle_is_owned_by_entry() -> None:
    source = _source("__init__.py")
    envelope_source = _source("network_envelope.py")
    tree = ast.parse(source)
    view = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "NetworkEnvelopeView"
    )
    methods = {
        node.name for node in view.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "get" in methods
    assert "post" not in methods
    assert 'url = "/api/power_sync/network_envelope"' in source
    assert '["network_envelope_manager"]' in source
    assert '["network_export_guard"]' in source
    assert "await network_envelope_manager.async_start()" in source
    assert "await network_envelope_manager.async_stop()" in source
    assert "async_track_time_interval" in envelope_source


def test_network_export_limit_sensor_tracks_atomic_manager_snapshot() -> None:
    source = _source("sensor.py")

    assert "class NetworkExportLimitSensor" in source
    assert 'f"{entry.entry_id}_network_export_limit"' in source
    assert "self._manager.snapshot.effective_limit_w" in source
    assert "snapshot.to_dict()" in source
    assert 'attributes["next_limit_w"]' in source
    assert "self._manager.add_listener" in source


def test_optimizer_and_runtime_export_paths_consume_the_same_envelope() -> None:
    coordinator = _source("optimization/coordinator.py")
    integration = _source("__init__.py")

    assert "optimizer_slot_limits(" in coordinator
    assert "grid_export_limits_w" in coordinator
    assert 'battery_export_allowed = [False] * len(import_prices)' in coordinator
    assert 'api_response["grid_export_limit_w"]' in coordinator
    assert 'data["network_envelope"]' in coordinator
    assert "_force_discharge_through_export_guard" in coordinator
    assert "guard.async_guard_write" in coordinator
    assert 'network_guard.clamp_requested_export_w' in integration
    assert "approve_reoptimized_snapshot" in integration
    assert '"network envelope source is stale or invalid"' in integration
    assert '"PCC telemetry is stale or unavailable"' in integration
    assert "await network_envelope_manager.async_set_fault(new.reason)" in integration
    assert "await guard.clamp_requested_export_w(0.0)" in integration
    assert "_network_envelope_blocks_unguarded_export_write" in integration
    assert "Self-consumption transition blocked by network envelope" in integration
    assert "_guarded_self_consumption_write" in integration
    assert "Autonomous/TOU mode blocked while the network envelope is" in integration
    assert "NetworkExportEnvelope()," in integration


def test_network_export_strings_and_translation_remain_in_sync() -> None:
    strings = json.loads(_source("strings.json"))
    english = json.loads(_source("translations/en.json"))

    for payload in (strings, english):
        options = payload["options"]
        assert options["step"]["init"]["menu_options"]["network_export"]
        step = options["step"]["network_export"]
        assert "CSIP-AUS" in step["description"]
        assert set(step["data"]) == {
            "network_export_mode",
            "network_export_limit_entity",
            "network_export_status_entity",
            "network_export_expiry_entity",
            "network_export_schedule_entity",
            "network_export_pcc_power_entity",
            "network_export_scope",
            "network_export_fallback_limit_w",
            "network_export_safety_margin_w",
            "network_export_site_phase_count",
            "network_export_all_der_attested",
        }
        assert payload["entity"]["sensor"]["network_export_limit"]["name"]

    assert strings["options"]["step"]["network_export"] == english["options"]["step"]["network_export"]
    assert strings["entity"]["sensor"]["network_export_limit"] == english["entity"]["sensor"]["network_export_limit"]
