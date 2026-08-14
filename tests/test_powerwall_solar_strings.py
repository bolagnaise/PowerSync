"""Powerwall solar string telemetry normalisation tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
INIT_PATH = ROOT / "__init__.py"


def _fleet_api_bms_module():
    spec = importlib.util.spec_from_file_location(
        "fleet_api_bms_under_test",
        ROOT / "powerwall_local" / "fleet_api_bms.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_solar_string_v1r_path_uses_local_ttl_and_skips_loopback():
    source = INIT_PATH.read_text()
    method_source = source[
        source.index("    async def _try_fleet_api_solar_strings_fetch"):
        source.index(
            "    async def get(self, request: web.Request)",
            source.index("    async def _try_fleet_api_solar_strings_fetch"),
        )
    ]

    assert "_sign_in_thread, 12" in method_source
    assert "data=signed_local" in method_source
    assert "not is_loopback_host(local_ip)" in method_source
    assert "_sign_in_thread, 300" in method_source
    assert "_b64.b64encode(signed_cloud)" in method_source


def test_pw3_components_strings_include_active_voltages_and_groups():
    mod = _fleet_api_bms_module()
    payload = {
        "components": {
            "pch": [
                {
                    "serialNumber": "PCH123",
                    "signals": [
                        {"name": "PCH_PvState_A", "textValue": "PV_Active"},
                        {"name": "PCH_PvVoltageA", "value": 295.2},
                        {"name": "PCH_PvCurrentA", "value": 3.1},
                        {"name": "PCH_PvState_B", "textValue": "PV_Active"},
                        {"name": "PCH_PvVoltageB", "value": 294.8},
                        {"name": "PCH_PvCurrentB", "value": 2.9},
                    ],
                }
            ]
        }
    }

    diagnostics = mod.normalize_pw3_components_strings(payload)

    assert diagnostics["source"] == "pw3_components"
    assert diagnostics["strings"][0]["label"] == "A"
    assert diagnostics["strings"][0]["voltage_v"] == 295.2
    assert round(diagnostics["strings"][0]["power_w"], 1) == 915.1
    assert diagnostics["groups"][0]["label"] == "MPPT A+B"
    assert diagnostics["groups"][0]["connected"] is True


def test_pw3_components_standby_only_is_suppressed():
    mod = _fleet_api_bms_module()
    payload = {
        "components": {
            "pch": [
                {
                    "signals": [
                        {"name": "PCH_PvState_A", "textValue": "PV_Standby"},
                        {"name": "PCH_PvVoltageA", "value": 14.2},
                        {"name": "PCH_PvCurrentA", "value": 0},
                        {"name": "PCH_PvState_B", "textValue": "PV_Standby"},
                        {"name": "PCH_PvVoltageB", "value": 14.1},
                        {"name": "PCH_PvCurrentB", "value": 0},
                    ],
                }
            ]
        }
    }

    assert mod.normalize_pw3_components_strings(payload) is None


def test_legacy_pvac_strings_use_pvs_connected_flags():
    mod = _fleet_api_bms_module()
    payload = {
        "esCan": {
            "bus": {
                "PVAC": [
                    {
                        "packageSerialNumber": "PVAC123",
                        "PVAC_Logging": {
                            "PVAC_PVMeasuredVoltage_A": 360.5,
                            "PVAC_PVCurrent_A": 4.2,
                            "PVAC_PVMeasuredVoltage_B": 0,
                            "PVAC_PVCurrent_B": 0,
                        },
                    }
                ],
                "PVS": [
                    {
                        "PVS_Status": {
                            "PVS_StringA_Connected": True,
                            "PVS_StringB_Connected": False,
                        }
                    }
                ],
            }
        }
    }

    diagnostics = mod.normalize_legacy_pvac_strings(payload)

    assert diagnostics["source"] == "legacy_pvac"
    assert [reading["label"] for reading in diagnostics["strings"]] == ["A"]
    assert diagnostics["strings"][0]["voltage_v"] == 360.5
    assert diagnostics["strings"][0]["connected"] is True
