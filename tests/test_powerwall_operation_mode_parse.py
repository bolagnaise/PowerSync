"""Regression test for PW-1: the operation-mode overlay must actually parse.

The Tesla Operation Mode select entity has a local-readback overlay that
reads ``PowerwallSnapshot.operation_mode`` from the local gateway config.
That field must be sourced from the TOP LEVEL of config.json's
``default_real_mode`` key — that's what the write path in
``__init__.py::handle_set_operation_mode`` writes ("default_real_mode lives
at the top level of config.json, not under site_info") and reads back.

This test goes through the real ``_snapshot_from_dcq`` parser (unlike
``tests/test_tesla_local_readback_overlay.py``, which injects
``operation_mode`` directly into a fake snapshot and would not catch a
parser regression) to make sure a config dict shaped like a real gateway's
config.json is parsed correctly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"
PKG = "power_sync_opmode_test"
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
# Stub Home Assistant modules for the local coordinator import.
ha_config_entries = types.ModuleType("homeassistant.config_entries")
ha_core = types.ModuleType("homeassistant.core")
ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")

ha_config_entries.ConfigEntry = type("ConfigEntry", (), {})
ha_core.HomeAssistant = type("HomeAssistant", (), {})


class _DataUpdateCoordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, *args, **kwargs):
        self.hass = hass
        self.data = None

    def async_add_listener(self, *args, **kwargs):
        return lambda: None


ha_update.DataUpdateCoordinator = _DataUpdateCoordinator
ha_update.UpdateFailed = Exception
sys.modules["homeassistant.config_entries"] = ha_config_entries
sys.modules["homeassistant.core"] = ha_core
sys.modules["homeassistant.helpers.update_coordinator"] = ha_update

const_stub = types.ModuleType(f"{PKG}.const")
const_stub.CONF_POWERWALL_LOCAL_PAIRED = "powerwall_local_paired"
const_stub.DOMAIN = "power_sync"
const_stub.POWERWALL_LOCAL_POLL_INTERVAL = 5
sys.modules[f"{PKG}.const"] = const_stub

# Stub modules the client imports but this test doesn't exercise.
exceptions_stub = types.ModuleType(f"{LOCAL_PKG}.exceptions")


class _PowerwallLocalError(Exception):
    pass


class _PowerwallUnreachableError(_PowerwallLocalError):
    pass


class _PowerwallAuthError(_PowerwallLocalError):
    pass


class _PowerwallPairingError(_PowerwallLocalError):
    pass


class _PowerwallSignatureError(_PowerwallLocalError):
    pass


exceptions_stub.PowerwallLocalError = _PowerwallLocalError
exceptions_stub.PowerwallUnreachableError = _PowerwallUnreachableError
exceptions_stub.PowerwallAuthError = _PowerwallAuthError
exceptions_stub.PowerwallPairingError = _PowerwallPairingError
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
    """A representative DeviceControllerQuery JSON payload (minimal)."""
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
            "alerts": {"active": []},
            "batteryBlocks": [],
            "siteShutdown": {"isShutDown": False, "reasons": []},
        }
    }


def test_operation_mode_parses_from_top_level_config_key():
    """Real gateways write default_real_mode at the TOP LEVEL of config.json.

    Proven by __init__.py::handle_set_operation_mode, which writes
    ``{"default_real_mode": mode}`` (undotted, i.e. top-level) and reads it
    back with ``config.get("default_real_mode")`` — not nested under
    ``site_info``. The snapshot parser must read the same location or the
    local-readback overlay silently never activates.
    """
    cfg = {
        "default_real_mode": "backup",
        "site_info": {
            "backup_reserve_percent": 20,
        },
    }

    snap = client_mod._snapshot_from_dcq(_sample_dcq(), cfg)

    assert snap.operation_mode == "backup"


def test_operation_mode_still_parses_from_site_info_fallback():
    """Some gateways may mirror default_real_mode under site_info too."""
    cfg = {
        "site_info": {
            "default_real_mode": "self_consumption",
            "backup_reserve_percent": 20,
        },
    }

    snap = client_mod._snapshot_from_dcq(_sample_dcq(), cfg)

    assert snap.operation_mode == "self_consumption"


def test_operation_mode_prefers_top_level_over_site_info_mirror():
    """If both locations are present, top-level (the real write target) wins."""
    cfg = {
        "default_real_mode": "backup",
        "site_info": {
            "default_real_mode": "self_consumption",
            "backup_reserve_percent": 20,
        },
    }

    snap = client_mod._snapshot_from_dcq(_sample_dcq(), cfg)

    assert snap.operation_mode == "backup"
