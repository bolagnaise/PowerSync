"""Test the DeviceControllerQuery → PowerwallSnapshot mapper."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
PKG = "power_sync_dcq_test"
LOCAL_PKG = f"{PKG}.powerwall_local"


def _ensure_test_package() -> None:
    if PKG in sys.modules:
        return
    pkg = types.ModuleType(PKG)
    pkg.__path__ = [str(ROOT)]
    sys.modules[PKG] = pkg
    local = types.ModuleType(LOCAL_PKG)
    local.__path__ = [str(ROOT / "powerwall_local")]
    sys.modules[LOCAL_PKG] = local


def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_ensure_test_package()
# Stub modules the client imports but the test doesn't exercise.
exceptions_stub = types.ModuleType(f"{LOCAL_PKG}.exceptions")
class _PowerwallLocalError(Exception):
    pass
class _PowerwallUnreachableError(_PowerwallLocalError):
    pass
class _PowerwallAuthError(_PowerwallLocalError):
    pass
class _PowerwallSignatureError(_PowerwallLocalError):
    pass
exceptions_stub.PowerwallLocalError = _PowerwallLocalError
exceptions_stub.PowerwallUnreachableError = _PowerwallUnreachableError
exceptions_stub.PowerwallAuthError = _PowerwallAuthError
exceptions_stub.PowerwallSignatureError = _PowerwallSignatureError
sys.modules[f"{LOCAL_PKG}.exceptions"] = exceptions_stub

# Stub fleet_api_bms — _snapshot_from_dcq doesn't use it, but client imports it.
fleet_stub = types.ModuleType(f"{LOCAL_PKG}.fleet_api_bms")
fleet_stub.build_device_controller_query_envelope = lambda din: b""
fleet_stub.parse_device_controller_response = lambda b: None
sys.modules[f"{LOCAL_PKG}.fleet_api_bms"] = fleet_stub

# Stub signaling.
signaling_stub = types.ModuleType(f"{LOCAL_PKG}.signaling")
class _TeslaSignalingClient:
    def __init__(self, *a, **kw):
        pass
signaling_stub.TeslaSignalingClient = _TeslaSignalingClient
sys.modules[f"{LOCAL_PKG}.signaling"] = signaling_stub

# Stub transport — just enough to not blow up the client import.
transport_stub = types.ModuleType(f"{LOCAL_PKG}.transport")
class _TEDAPIv1rTransport:
    def __init__(self, *a, **kw):
        pass
    @property
    def din(self):
        return None
transport_stub.TEDAPIv1rTransport = _TEDAPIv1rTransport
sys.modules[f"{LOCAL_PKG}.transport"] = transport_stub

client_mod = _load_module(
    f"{LOCAL_PKG}.client",
    ROOT / "powerwall_local" / "client.py",
)


def _sample_dcq() -> dict:
    """A representative DeviceControllerQuery JSON payload."""
    return {
        "control": {
            "meterAggregates": [
                {"location": "site", "realPowerW": 1234.5},
                {"location": "battery", "realPowerW": -2000.0},
                {"location": "solar", "realPowerW": 4500.0},
                {"location": "load", "realPowerW": 3500.0},
            ],
            "systemStatus": {
                "nominalFullPackEnergyWh": 27000.0,
                "nominalEnergyRemainingWh": 8100.0,
            },
            "islanding": {
                "customerIslandMode": "OnGrid",
                "contactorClosed": True,
                "microGridOK": True,
                "gridOK": True,
                "disableReasons": [],
            },
            "alerts": {"active": ["DummyAlert"]},
            "batteryBlocks": [
                {"din": "PW3--SN1", "disableReasons": []},
                {"din": "PW3--SN2", "disableReasons": []},
            ],
            "siteShutdown": {"isShutDown": False, "reasons": []},
        }
    }


def _sample_cfg() -> dict:
    return {
        "site_info": {
            "default_real_mode": "self_consumption",
            "backup_reserve_percent": 20,
        }
    }


def test_snapshot_from_dcq_full_payload():
    snap = client_mod._snapshot_from_dcq(_sample_dcq(), _sample_cfg())
    assert snap.solar_w == 4500.0
    assert snap.battery_w == -2000.0
    assert snap.grid_w == 1234.5
    assert snap.load_w == 3500.0
    # PowerSync reports Tesla app SOC, scaled across the usable 5-100% range.
    assert snap.soc == 26.31578947368421
    assert snap.grid_status == "SystemGridConnected"
    assert snap.operation_mode == "self_consumption"
    assert snap.backup_reserve_percent == 20
    assert snap.pw_count == 2
    assert snap.total_pack_full_wh == 27000.0
    assert snap.total_pack_remaining_wh == 8100.0
    assert snap.battery_blocks is not None and len(snap.battery_blocks) == 2
    assert snap.alerts == [{"name": "DummyAlert"}]


def test_snapshot_from_dcq_missing_meters():
    """Locations the gateway didn't return should map to None, not crash."""
    dcq = _sample_dcq()
    dcq["control"]["meterAggregates"] = [
        {"location": "site", "realPowerW": 100.0},
    ]
    snap = client_mod._snapshot_from_dcq(dcq, _sample_cfg())
    assert snap.grid_w == 100.0
    assert snap.solar_w is None
    assert snap.battery_w is None
    assert snap.load_w is None


def test_snapshot_from_dcq_uppercase_meter_locations():
    """PW3 DCQ can return uppercase meter locations."""
    dcq = _sample_dcq()
    for meter in dcq["control"]["meterAggregates"]:
        meter["location"] = meter["location"].upper()
    snap = client_mod._snapshot_from_dcq(dcq, _sample_cfg())
    assert snap.grid_w == 1234.5
    assert snap.battery_w == -2000.0
    assert snap.solar_w == 4500.0
    assert snap.load_w == 3500.0


def test_snapshot_from_dcq_no_config():
    """A failed config.json read should leave operation_mode/backup_reserve None."""
    snap = client_mod._snapshot_from_dcq(_sample_dcq(), None)
    assert snap.operation_mode is None
    assert snap.backup_reserve_percent is None
    # Other fields still populate from DCQ.
    assert snap.soc == 26.31578947368421
    assert snap.grid_w == 1234.5


def test_snapshot_from_dcq_off_grid_mode():
    dcq = _sample_dcq()
    dcq["control"]["islanding"]["customerIslandMode"] = "OffGrid"
    dcq["control"]["islanding"]["gridOK"] = False
    snap = client_mod._snapshot_from_dcq(dcq, _sample_cfg())
    assert snap.grid_status == "SystemIslandedActive"


def test_snapshot_from_dcq_zero_full_pack_energy():
    """SOC must stay None when full-pack energy is missing or zero."""
    dcq = _sample_dcq()
    dcq["control"]["systemStatus"]["nominalFullPackEnergyWh"] = 0
    snap = client_mod._snapshot_from_dcq(dcq, None)
    assert snap.soc is None


def test_snapshot_from_dcq_empty_alerts():
    dcq = _sample_dcq()
    dcq["control"]["alerts"] = {"active": []}
    snap = client_mod._snapshot_from_dcq(dcq, None)
    assert snap.alerts == []
