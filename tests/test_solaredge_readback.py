"""Tests for guarded SolarEdge Modbus Multi register readback."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parent.parent / "custom_components" / "power_sync"

power_sync = sys.modules.setdefault("power_sync", types.ModuleType("power_sync"))
power_sync.__path__ = [str(ROOT)]
inverters = sys.modules.setdefault(
    "power_sync.inverters", types.ModuleType("power_sync.inverters")
)
inverters.__path__ = [str(ROOT / "inverters")]


class TimestampDataUpdateCoordinator:
    pass


@dataclass
class _Entry:
    platform: str = "solaredge_modbus_multi"
    config_entry_id: str = "entry-a"
    device_id: str = "device-a"
    unique_id: str = "SE5000_SERIAL-A_storage_command_mode"


@dataclass
class _Device:
    config_entries: set[str]
    identifiers: set[tuple[str, str]]


class _Registry:
    def __init__(self, value):
        self.value = value

    def async_get(self, key):
        return self.value


class _Coordinator(TimestampDataUpdateCoordinator):
    def __init__(
        self,
        *,
        advances=True,
        succeeds=True,
        replaces_storage=True,
        delayed=False,
    ):
        self.last_update_success_time = 1
        self.last_update_success = True
        self.advances = advances
        self.succeeds = succeeds
        self.replaces_storage = replaces_storage
        self.delayed = delayed
        self.refreshes = 0
        self.listeners = []

    async def async_request_refresh(self):
        self.refreshes += 1
        if self.delayed:
            asyncio.create_task(self._complete_refresh())
            return
        await self._complete_refresh()

    async def _complete_refresh(self):
        if self.delayed:
            await asyncio.sleep(0)
        self.last_update_success = self.succeeds
        if self.advances and self.succeeds:
            self.last_update_success_time += 1
            if self.replaces_storage:
                for inverter in self._hub.inverters:
                    inverter.decoded_storage_control = dict(
                        inverter.decoded_storage_control
                    )
        for listener in tuple(self.listeners):
            listener()

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def unsubscribe():
            self.listeners.remove(listener)

        return unsubscribe


class _Inverter:
    def __init__(self, uid, command_mode=7):
        self.uid_base = uid
        self.decoded_storage_control = {
            "control_mode": 4,
            "ac_charge_policy": 1,
            "ac_charge_limit": 12.5,
            "backup_reserve": 20.0,
            "default_mode": 7,
            "command_timeout": 3600,
            "command_mode": command_mode,
            "charge_limit": 4200.0,
            "discharge_limit": 3800.0,
        }


class _Hub:
    def __init__(self, inverters):
        self.inverters = inverters
        self.has_write = None
        self.option_storage_control = True


def _install_ha_modules(monkeypatch, entry, device):
    helpers = types.ModuleType("homeassistant.helpers")
    er = types.ModuleType("homeassistant.helpers.entity_registry")
    dr = types.ModuleType("homeassistant.helpers.device_registry")
    uc = types.ModuleType("homeassistant.helpers.update_coordinator")
    er.async_get = lambda hass: _Registry(entry)
    dr.async_get = lambda hass: _Registry(device)
    uc.TimestampDataUpdateCoordinator = TimestampDataUpdateCoordinator
    helpers.entity_registry = er
    helpers.device_registry = dr
    modules = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": er,
        "homeassistant.helpers.device_registry": dr,
        "homeassistant.helpers.update_coordinator": uc,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    custom_components = types.ModuleType("custom_components")
    upstream = types.ModuleType("custom_components.solaredge_modbus_multi")
    const = types.ModuleType("custom_components.solaredge_modbus_multi.const")
    const.STORAGE_CONTROL_MODE = {0: "Disabled", 4: "Remote Control"}
    const.STORAGE_AC_CHARGE_POLICY = {0: "Disabled", 1: "Always Allowed"}
    const.STORAGE_MODE = {
        0: "Solar Power Only (Off)",
        7: "Maximize Self Consumption",
    }
    monkeypatch.setitem(sys.modules, "custom_components", custom_components)
    monkeypatch.setitem(sys.modules, "custom_components.solaredge_modbus_multi", upstream)
    monkeypatch.setitem(sys.modules, "custom_components.solaredge_modbus_multi.const", const)


def _hass(coordinator, inverters):
    hub = _Hub(inverters)
    coordinator._hub = hub
    return types.SimpleNamespace(
        data={
            "solaredge_modbus_multi": {
                "entry-a": {"hub": hub, "coordinator": coordinator}
            }
        }
    )


def _read(monkeypatch, coordinator, inverters, entry=None, device=None):
    entry = entry or _Entry()
    device = device or _Device(
        {"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")}
    )
    _install_ha_modules(monkeypatch, entry, device)
    from power_sync.inverters.solaredge_readback import async_read_storage_state

    return asyncio.run(
        async_read_storage_state(_hass(coordinator, inverters), "select.command")
    )


def test_readback_rejects_stale_refresh(monkeypatch):
    coordinator = _Coordinator(advances=False)
    assert _read(monkeypatch, coordinator, [_Inverter("SE5000_SERIAL-A")]) is None
    assert coordinator.refreshes == 1


def test_readback_rejects_failed_poll(monkeypatch):
    coordinator = _Coordinator(succeeds=False)
    assert _read(monkeypatch, coordinator, [_Inverter("SE5000_SERIAL-A")]) is None
    assert coordinator.refreshes == 1


def test_readback_rejects_poll_that_did_not_read_storage_registers(monkeypatch):
    coordinator = _Coordinator(replaces_storage=False)
    assert _read(monkeypatch, coordinator, [_Inverter("SE5000_SERIAL-A")]) is None
    assert coordinator.refreshes == 1


def test_readback_waits_for_debounced_poll_completion(monkeypatch):
    coordinator = _Coordinator(delayed=True)
    result = _read(monkeypatch, coordinator, [_Inverter("SE5000_SERIAL-A")])
    assert result is not None
    assert result["command_mode"] == "Maximize Self Consumption"
    assert coordinator.refreshes == 1
    assert coordinator.listeners == []


def test_readback_rejects_wrong_identity_in_multi_inverter_hub(monkeypatch):
    result = _read(
        monkeypatch,
        _Coordinator(),
        [_Inverter("SE5000_SERIAL-B"), _Inverter("SE5000_SERIAL-C")],
    )
    assert result is None


def test_readback_returns_copied_translated_state_without_mutation(monkeypatch):
    inverter = _Inverter("SE5000_SERIAL-A")
    coordinator = _Coordinator()
    hass = _hass(coordinator, [inverter, _Inverter("SE5000_SERIAL-B", command_mode=0)])
    entry = _Entry()
    device = _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")})
    _install_ha_modules(monkeypatch, entry, device)
    before_data = dict(inverter.decoded_storage_control)
    before_hass_data = {key: id(value) for key, value in hass.data.items()}

    from power_sync.inverters.solaredge_readback import async_read_storage_state

    result = asyncio.run(async_read_storage_state(hass, "select.command"))

    assert result == {
        "control_mode": "Remote Control",
        "ac_charge_policy": "Always Allowed",
        "ac_charge_limit": 12.5,
        "backup_reserve": 20.0,
        "default_mode": "Maximize Self Consumption",
        "command_timeout": 3600,
        "command_mode": "Maximize Self Consumption",
        "charge_limit": 4200.0,
        "discharge_limit": 3800.0,
    }
    result["charge_limit"] = 0
    assert inverter.decoded_storage_control == before_data
    assert {key: id(value) for key, value in hass.data.items()} == before_hass_data
    assert coordinator.refreshes == 1


@pytest.mark.parametrize("field", ["ac_charge_limit", "default_mode", "ac_charge_policy", "backup_reserve"])
@pytest.mark.parametrize("value", [None, -1, float("nan"), True])
def test_readback_ignores_unsupported_optional_field(monkeypatch, field, value):
    inverter = _Inverter("SE5000_SERIAL-A")
    if value is None:
        inverter.decoded_storage_control.pop(field)
    else:
        inverter.decoded_storage_control[field] = value
    result = _read(monkeypatch, _Coordinator(), [inverter])
    assert result is not None
    assert result["command_mode"] == "Maximize Self Consumption"
    assert field not in result


@pytest.mark.parametrize("field", ["control_mode", "command_mode", "command_timeout", "charge_limit", "discharge_limit"])
@pytest.mark.parametrize("value", [None, float("nan"), True, -1])
def test_readback_rejects_invalid_essential_field(monkeypatch, field, value):
    inverter = _Inverter("SE5000_SERIAL-A")
    if value is None:
        inverter.decoded_storage_control.pop(field)
    else:
        inverter.decoded_storage_control[field] = value
    assert _read(monkeypatch, _Coordinator(), [inverter]) is None


@pytest.mark.parametrize("field", ["control_mode", "command_mode"])
def test_readback_rejects_unknown_required_enum(monkeypatch, field):
    inverter = _Inverter("SE5000_SERIAL-A")
    inverter.decoded_storage_control[field] = 65535
    assert _read(monkeypatch, _Coordinator(), [inverter]) is None


@pytest.mark.parametrize("field", ["ac_charge_policy", "default_mode"])
def test_readback_omits_unknown_optional_enum(monkeypatch, field):
    inverter = _Inverter("SE5000_SERIAL-A")
    inverter.decoded_storage_control[field] = 65535
    result = _read(monkeypatch, _Coordinator(), [inverter])
    assert result is not None
    assert field not in result


def test_readback_baseline_omits_unsupported_optional_fields(monkeypatch):
    inverter = _Inverter("SE5000_SERIAL-A")
    inverter.decoded_storage_control.pop("backup_reserve")
    inverter.decoded_storage_control["ac_charge_policy"] = 65535
    _install_ha_modules(
        monkeypatch, _Entry(),
        _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")}),
    )
    from power_sync.inverters.solaredge_readback import async_read_storage_baseline

    result = asyncio.run(async_read_storage_baseline(
        _hass(_Coordinator(), [inverter]), "select.command"
    ))
    assert result == {
        "storage_control_mode": "Remote Control",
        "storage_command_mode": "Maximize Self Consumption",
        "command_timeout": 3600,
        "charge_power_limit": 4200.0,
        "discharge_power_limit": 3800.0,
    }


@pytest.mark.parametrize(
    ("keep_open", "connected", "expected_delay"),
    [(False, False, True), (True, False, False), (False, True, False),
     (None, False, False), (False, None, False), (None, None, False)],
)
def test_readback_transport_settle_only_for_explicitly_closed_connection(
    monkeypatch, keep_open, connected, expected_delay
):
    from unittest.mock import AsyncMock
    from power_sync.inverters import solaredge_readback

    coordinator = _Coordinator()
    hass = _hass(coordinator, [_Inverter("SE5000_SERIAL-A")])
    coordinator._hub.keep_modbus_open = keep_open
    coordinator._hub.is_connected = connected
    _install_ha_modules(
        monkeypatch, _Entry(),
        _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")}),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(solaredge_readback.asyncio, "sleep", sleep)
    result = asyncio.run(solaredge_readback.async_read_storage_state(hass, "select.command"))
    assert result is not None
    if expected_delay:
        sleep.assert_awaited_once_with(2.0)
    else:
        sleep.assert_not_awaited()
    assert coordinator.refreshes == 1
    assert coordinator.listeners == []


@pytest.mark.parametrize("interruption", ["write", "failed_poll", "cancel", "timeout"])
def test_readback_transport_settle_preserves_failure_and_cancellation(
    monkeypatch, interruption
):
    from unittest.mock import AsyncMock
    from power_sync.inverters import solaredge_readback

    coordinator = _Coordinator()
    hass = _hass(coordinator, [_Inverter("SE5000_SERIAL-A")])
    coordinator._hub.keep_modbus_open = False
    coordinator._hub.is_connected = False
    _install_ha_modules(
        monkeypatch, _Entry(),
        _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")}),
    )

    async def settle(seconds):
        if interruption == "write":
            coordinator._hub.has_write = True
        elif interruption == "failed_poll":
            coordinator.last_update_success = False
        elif interruption == "cancel":
            raise asyncio.CancelledError
        else:
            raise TimeoutError

    sleep = AsyncMock(side_effect=settle)
    monkeypatch.setattr(solaredge_readback.asyncio, "sleep", sleep)
    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(solaredge_readback.async_read_storage_state(hass, "select.command"))
    else:
        assert asyncio.run(solaredge_readback.async_read_storage_state(hass, "select.command")) is None
    sleep.assert_awaited_once_with(2.0)
    assert coordinator.refreshes == 1
    assert coordinator.listeners == []


def test_readback_transport_settle_uses_remaining_refresh_deadline(monkeypatch):
    from unittest.mock import AsyncMock, Mock
    from power_sync.inverters import solaredge_readback

    coordinator = _Coordinator()
    hass = _hass(coordinator, [_Inverter("SE5000_SERIAL-A")])
    coordinator._hub.keep_modbus_open = False
    coordinator._hub.is_connected = False
    _install_ha_modules(
        monkeypatch, _Entry(),
        _Device({"entry-a"}, {("solaredge_modbus_multi", "SE5000_SERIAL-A")}),
    )
    wait_for = AsyncMock(side_effect=asyncio.wait_for)
    monkeypatch.setattr(solaredge_readback.asyncio, "wait_for", wait_for)
    monkeypatch.setattr(solaredge_readback.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(solaredge_readback, "_remaining", Mock(side_effect=[30.0, 0.5]))
    assert asyncio.run(solaredge_readback.async_read_storage_state(hass, "select.command")) is not None
    assert [call.kwargs["timeout"] for call in wait_for.await_args_list] == [30.0, 0.5]
