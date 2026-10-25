"""Common API failures must remain inspectable in HA's unavailable state."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def capability_attributes(self):
        return getattr(self.coordinator, "sensor_capabilities", None)


def _sensor_classes():
    """Execute the real sensor classes without importing the integration."""
    tree = ast.parse((ROOT / "sensor.py").read_text())
    names = {
        "_PowerwallLocalSensorBase", "_PowerwallV1rSensorBase",
        "PowerwallV1rDeviceSensor", "PowerwallV1rFirmwareSensor",
        "PowerwallV1rNetworkSensor", "PowerwallV1rInternetSensor",
    }
    nodes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name in names]
    namespace = {
        "CoordinatorEntity": _CoordinatorEntity,
        "SensorEntity": type("SensorEntity", (), {}),
        "ConfigEntry": object,
        "Any": Any,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(ROOT / "sensor.py"), "exec"), namespace)
    return namespace


SENSORS = _sensor_classes()


def _ha_state(sensor):
    # HA Entity.__async_calculate_state always publishes capability_attributes,
    # but reads extra_state_attributes only when available. See core
    # homeassistant/helpers/entity.py (2026.10), also true before this release.
    attrs = dict(sensor.capability_attributes or {})
    if not sensor.available:
        return "unavailable", attrs
    attrs.update(sensor.extra_state_attributes or {})
    return sensor.native_value, attrs


@pytest.mark.parametrize("sensor_name,key", [
    ("PowerwallV1rDeviceSensor", "system_info"),
    ("PowerwallV1rFirmwareSensor", "system_info"),
    ("PowerwallV1rNetworkSensor", "networking"),
    ("PowerwallV1rInternetSensor", "internet"),
])
@pytest.mark.parametrize("whole_refresh_failure", [False, True])
def test_failed_endpoint_publishes_attempt_and_error_while_unavailable(sensor_name, key, whole_refresh_failure):
    diagnostics = {
        "system_info": None, "networking": None, "internet": None,
        "last_attempt_ts": 200.0, "last_success_ts": 100.0,
        "errors": {"diagnostics" if whole_refresh_failure else key: "gateway busy"},
    }
    coord = SimpleNamespace(_v1r_diagnostics=diagnostics, last_update_success=False)
    sensor = SENSORS[sensor_name](coord, SimpleNamespace(entry_id="entry"))

    state, attrs = _ha_state(sensor)

    assert state == "unavailable"
    assert attrs == {
        "last_attempt_ts": 200.0, "last_success_ts": 100.0, "error": "gateway busy",
    }


def test_diagnostic_recovery_and_partial_failure_do_not_depend_on_dcq():
    coord = SimpleNamespace(last_update_success=False, _v1r_diagnostics={
        "system_info": {"device_type": "Gateway 3", "firmware_version": "test"},
        "networking": None, "internet": None,
        "last_attempt_ts": 200.0, "last_success_ts": 200.0,
        "errors": {"networking": "unsupported field"},
    })
    entry = SimpleNamespace(entry_id="entry")
    device = SENSORS["PowerwallV1rDeviceSensor"](coord, entry)
    network = SENSORS["PowerwallV1rNetworkSensor"](coord, entry)

    assert _ha_state(device) == ("Gateway 3", {
        "last_attempt_ts": 200.0, "last_success_ts": 200.0,
    })
    assert _ha_state(network)[1]["error"] == "unsupported field"
    coord._v1r_diagnostics = {
        "system_info": None, "networking": {"wifi": {"active_route": True}},
        "last_attempt_ts": 500.0, "last_success_ts": 500.0, "errors": {},
    }
    assert _ha_state(network) == ("wifi", {
        "last_attempt_ts": 500.0, "last_success_ts": 500.0,
        "interfaces": {"wifi": {"active_route": True}},
    })
    assert _ha_state(device)[0] == "unavailable"
    assert "error" not in _ha_state(device)[1]


def test_unattempted_diagnostics_remain_unavailable_without_invented_values():
    coord = SimpleNamespace(_v1r_diagnostics={}, last_update_success=False)
    sensor = SENSORS["PowerwallV1rDeviceSensor"](coord, SimpleNamespace(entry_id="entry"))
    assert _ha_state(sensor) == ("unavailable", {})


def test_failed_endpoint_preserves_inherited_sensor_capabilities():
    coord = SimpleNamespace(
        sensor_capabilities={"inherited_capability": "retained"},
        _v1r_diagnostics={
            "last_attempt_ts": 200.0, "errors": {"system_info": "empty reply"},
        },
    )
    sensor = SENSORS["PowerwallV1rDeviceSensor"](coord, SimpleNamespace(entry_id="entry"))
    assert _ha_state(sensor) == ("unavailable", {
        "inherited_capability": "retained", "last_attempt_ts": 200.0,
        "error": "empty reply",
    })
    assert coord.sensor_capabilities == {"inherited_capability": "retained"}
