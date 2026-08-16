"""Regression tests for V1R Powerwall alert classification."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "custom_components" / "power_sync" / "tesla_alerts.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("power_sync_tesla_alerts", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ALERTS = _load_module()


def test_standard_powerwall_status_records_are_not_actionable():
    raw = [
        {"name": "SystemConnectedToGrid"},
        {"name": "FWUpdateSucceeded"},
        {"name": "GridCodesWrite"},
        {"name": "PodCommissionTime"},
    ]

    actionable, informational = ALERTS.split_powerwall_alerts(raw)

    assert actionable == []
    assert informational == raw


def test_calibration_is_not_duplicated_as_a_generic_problem():
    actionable, informational = ALERTS.split_powerwall_alerts(
        [{"name": "BatteryCalibration"}]
    )

    assert actionable == []
    assert informational == [{"name": "BatteryCalibration"}]


def test_explicit_non_error_severity_is_filtered_without_name_allowlisting():
    actionable, informational = ALERTS.split_powerwall_alerts(
        [{"name": "FirmwareOperation", "severity": "Informational"}]
    )

    assert actionable == []
    assert informational == [
        {"name": "FirmwareOperation", "severity": "Informational"}
    ]


def test_unknown_and_site_meter_comms_alerts_remain_actionable():
    raw = [
        {"name": "SiteMeterComms", "severity": "performance"},
        {"name": "FutureTeslaAlert"},
    ]

    actionable, informational = ALERTS.split_powerwall_alerts(raw)

    assert actionable == raw
    assert informational == []


def test_attributes_exclude_normal_records_but_retain_raw_evidence():
    raw = [
        {"name": "FWUpdateSucceeded"},
        {"alert_name": "BatteryFault", "alert_severity": "critical"},
    ]

    attributes = ALERTS.powerwall_alert_attributes(raw)

    assert attributes["alerts"] == ["BatteryFault"]
    assert attributes["severities"] == {"BatteryFault": "critical"}
    assert attributes["informational_alerts"] == ["FWUpdateSucceeded"]
    assert attributes["informational_severities"] == {}
    assert attributes["all_alerts"] == ["FWUpdateSucceeded", "BatteryFault"]
    assert attributes["alert_details"] == [raw[1]]
    assert attributes["informational_alert_details"] == [raw[0]]


def test_sensor_and_binary_sensor_use_shared_classification():
    sensor_source = (ROOT / "custom_components" / "power_sync" / "sensor.py").read_text()
    binary_source = (
        ROOT / "custom_components" / "power_sync" / "binary_sensor.py"
    ).read_text()

    assert "split_powerwall_alerts(snap.alerts)" in sensor_source
    assert "powerwall_alert_attributes(snap.alerts)" in sensor_source
    assert "split_powerwall_alerts(snap.alerts)" in binary_source
    assert "powerwall_alert_attributes(snap.alerts)" in binary_source
