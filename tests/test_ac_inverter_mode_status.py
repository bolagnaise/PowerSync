"""Regression tests for AC inverter control mode visibility."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONST_PATH = ROOT / "custom_components" / "power_sync" / "const.py"
INIT_PATH = ROOT / "custom_components" / "power_sync" / "__init__.py"
SENSOR_PATH = ROOT / "custom_components" / "power_sync" / "sensor.py"


def test_ac_inverter_control_mode_constants_are_shared():
    const_source = CONST_PATH.read_text()

    assert 'INVERTER_CONTROL_MODE_NORMAL = "normal"' in const_source
    assert 'INVERTER_CONTROL_MODE_LOAD_FOLLOWING = "load_following"' in const_source
    assert 'INVERTER_CONTROL_MODE_SHUTDOWN = "shutdown"' in const_source
    assert 'INVERTER_CONTROL_MODE_CURTAILED = "curtailed"' in const_source
    assert "INVERTER_CONTROL_MODES = {" in const_source


def test_runtime_paths_write_inverter_control_mode_and_target():
    init_source = INIT_PATH.read_text()

    assert "def _set_inverter_control_state(" in init_source
    assert 'entry_data["inverter_control_mode"] = mode' in init_source
    assert 'entry_data["inverter_last_state"] = (' in init_source
    assert 'entry_data["inverter_power_limit_w"] = (' in init_source
    assert "INVERTER_CONTROL_MODE_LOAD_FOLLOWING" in init_source
    assert "INVERTER_CONTROL_MODE_SHUTDOWN" in init_source
    assert "INVERTER_CONTROL_MODE_NORMAL" in init_source


def test_manual_curtailment_keeps_specific_mode_for_refresh_loop():
    init_source = INIT_PATH.read_text()

    assert 'requested_mode = call.data.get("mode", INVERTER_CONTROL_MODE_LOAD_FOLLOWING)' in init_source
    assert "if requested_mode == INVERTER_CONTROL_MODE_SHUTDOWN" in init_source
    assert 'hass.data[DOMAIN][entry.entry_id]["inverter_controller"] = controller' in init_source
    assert "_set_inverter_control_state(" in init_source
    assert "update_dpel_time=inverter_brand == \"enphase\"" in init_source


def test_restore_clears_control_mode_and_cached_controller():
    init_source = INIT_PATH.read_text()

    assert "_set_inverter_control_state(INVERTER_CONTROL_MODE_NORMAL)" in init_source
    assert 'pop("inverter_controller", None)' in init_source
    assert 'entry_data["last_dpel_update_time"] = None' in init_source


def test_enphase_dpel_refresh_runs_at_15_seconds_without_speeding_other_brands():
    init_source = INIT_PATH.read_text()

    assert "timedelta(seconds=15)" in init_source
    assert "second=[0, 15, 30, 45]" in init_source
    assert 'inverter_brand != "enphase" and getattr(now, "second", None) not in (0, 30)' in init_source
    assert "INVERTER_CONTROL_MODE_SHUTDOWN" in init_source
    assert "await controller.curtail()" in init_source
    assert "Fast load-following update scheduled every 15 seconds for Enphase" in init_source


def test_inverter_status_sensor_exposes_mode_target_and_specific_states():
    sensor_source = SENSOR_PATH.read_text()

    assert '"control_mode": control_mode' in sensor_source
    assert '"target_power_w": target_power_w' in sensor_source
    assert 'return "Load Following"' in sensor_source
    assert 'return "Shutdown"' in sensor_source
    assert 'return "Curtailed"' in sensor_source
    assert "Inverter curtailed - load following at {target_power_w}W" in sensor_source
    assert "Inverter curtailed - shutdown mode" in sensor_source
    assert "Inverter operating normally" in sensor_source


def test_sensor_polling_preserves_specific_control_modes():
    sensor_source = SENSOR_PATH.read_text()

    assert "control_mode = entry_data.get(\"inverter_control_mode\")" in sensor_source
    assert "INVERTER_CONTROL_MODE_LOAD_FOLLOWING" in sensor_source
    assert "INVERTER_CONTROL_MODE_SHUTDOWN" in sensor_source
    assert 'entry_data["inverter_last_state"] = "curtailed"' in sensor_source
    assert 'entry_data["inverter_control_mode"] = INVERTER_CONTROL_MODE_NORMAL' in sensor_source
