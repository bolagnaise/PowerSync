"""Regression tests for Fronius load-following capacity detection."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_package_stubs() -> None:
    pymodbus = types.ModuleType("pymodbus")
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")

    class AsyncModbusTcpClient:
        pass

    class ModbusException(Exception):
        pass

    pymodbus_client.AsyncModbusTcpClient = AsyncModbusTcpClient
    pymodbus_exceptions.ModbusException = ModbusException
    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions

    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


_install_package_stubs()

from power_sync.inverters.fronius import FroniusController  # noqa: E402


def _model_registers(model: str) -> list[int]:
    raw = model.encode("ascii").ljust(32, b"\x00")
    return [int.from_bytes(raw[index : index + 2], "big") for index in range(0, 32, 2)]


def _controller_with_model(model: str) -> FroniusController:
    controller = FroniusController("192.0.2.1", load_following=True)

    async def connect() -> bool:
        return True

    async def read_register(address: int, count: int = 1):
        assert address == controller.REG_MODEL
        assert count == 16
        return _model_registers(model)

    controller.connect = connect
    controller._read_register = read_register
    return controller


def test_gen24_model_uses_power_rating_not_generation_number():
    cases = {
        "Primo GEN24 10.0 Plus": 10_000,
        "Symo GEN24 6.0 Plus": 6_000,
    }

    for model, expected_capacity in cases.items():
        controller = _controller_with_model(model)
        assert asyncio.run(controller.get_rated_capacity()) == expected_capacity


def test_legacy_model_capacity_parsing_is_preserved():
    controller = _controller_with_model("Symo 8.2-3-M")

    assert asyncio.run(controller.get_rated_capacity()) == 8_200


def _controller_with_control_readback(
    *,
    enabled: int,
    limit_value: int,
    ac_power: int = 2_078,
) -> tuple[FroniusController, list[tuple[int, int]]]:
    controller = FroniusController("192.0.2.1", load_following=True)
    writes: list[tuple[int, int]] = []

    async def connect() -> bool:
        return True

    async def write_register(address: int, value: int) -> bool:
        writes.append((address, value))
        return True

    async def read_register(address: int, count: int = 1):
        if address == controller.REG_WMAXLIM_ENA:
            return [enabled]
        if address == controller.REG_WMAXLIMPCT:
            return [limit_value]
        if address == controller.REG_WMAXLIMPCT_RVRT:
            return [0]
        if address == controller.REG_AC_POWER:
            assert count == 2
            return [ac_power, 0]
        if address == controller.REG_DC_POWER:
            return [0]
        if address == controller.REG_TEMPERATURE:
            return [250]
        if address == controller.REG_STATUS:
            return [controller.STATUS_MPPT]
        raise AssertionError(address)

    controller.connect = connect
    controller._write_register = write_register
    controller._read_register = read_register
    return controller, writes


@pytest.fixture(autouse=True)
def _skip_fronius_settle_delays(monkeypatch):
    async def no_sleep(_seconds: float) -> None:
        return None

    fronius_module = sys.modules["power_sync.inverters.fronius"]
    monkeypatch.setattr(fronius_module.asyncio, "sleep", no_sleep)


def test_load_following_requires_matching_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=5_000,
    )

    result = asyncio.run(
        controller.curtail(home_load_w=1_440, rated_capacity_w=5_000)
    )

    assert result is False
    assert writes == [
        (controller.REG_WMAXLIM_ENA, 0),
        (controller.REG_WMAXLIMPCT, 2_880),
        (controller.REG_WMAXLIMPCT_RVRT, 0),
        (controller.REG_WMAXLIM_ENA, 1),
    ]


def test_load_following_accepts_matching_enabled_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_880,
    )

    result = asyncio.run(
        controller.curtail(home_load_w=1_440, rated_capacity_w=5_000)
    )

    assert result is True
    assert writes[-1] == (controller.REG_WMAXLIM_ENA, 1)


def test_fronius_status_exposes_confirmed_limit_percentage():
    controller, _writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_830,
    )

    state = asyncio.run(controller.get_status())

    assert state.is_curtailed is True
    assert state.power_limit_percent == pytest.approx(28.3)
    assert state.power_output_w == pytest.approx(2_078)


def test_restore_requires_matching_unrestricted_limit_readback():
    controller, writes = _controller_with_control_readback(
        enabled=1,
        limit_value=2_830,
    )

    result = asyncio.run(controller.restore())

    assert result is False
    assert writes == [
        (controller.REG_WMAXLIM_ENA, 0),
        (controller.REG_WMAXLIMPCT, 10_000),
        (controller.REG_WMAXLIM_ENA, 1),
    ]


def test_restore_accepts_matching_unrestricted_limit_readback():
    controller, _writes = _controller_with_control_readback(
        enabled=1,
        limit_value=10_000,
    )

    assert asyncio.run(controller.restore()) is True


@pytest.fixture
def native_site(monkeypatch):
    from types import SimpleNamespace as NS

    values = {"number.renamed_export": "5000"}
    saved = {}
    calls = []
    entries = [NS(entry_id="upstream", data={"host": "192.0.2.1"},
                  options={}, state=NS(value="loaded"))]
    rows = [NS(entity_id="number.renamed_export", unique_id="primo_export_soft_limit",
               disabled_by=None)]
    registry = types.ModuleType("homeassistant.helpers.entity_registry")
    registry.async_get = lambda hass: None
    registry.async_entries_for_config_entry = lambda reg, eid: rows if eid == "upstream" else []
    storage = types.ModuleType("homeassistant.helpers.storage")

    class Store:
        def __init__(self, hass, version, key, **kwargs):
            self.key = key
        async def async_load(self):
            return saved.get(self.key)
        async def async_save(self, value):
            saved[self.key] = dict(value)

    storage.Store = Store
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_registry", registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.storage", storage)
    helpers = types.ModuleType("homeassistant.helpers")
    helpers.entity_registry = registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)

    def state(eid):
        if eid not in values:
            return None
        return NS(state=values[eid], attributes={"unit_of_measurement": "W", "min": 0, "max": 10000})
    async def service(domain, name, data, blocking):
        calls.append(dict(data))
        values[data["entity_id"]] = str(data["value"])

    hass = NS(states=NS(get=state), services=NS(async_call=service),
              config_entries=NS(async_entries=lambda domain: entries))
    return NS(hass=hass, values=values, saved=saved, calls=calls, entries=entries, rows=rows)


def _native_controller(site):
    controller, writes = _controller_with_control_readback(enabled=1, limit_value=10000)
    # Use the same dependency injection as the production factory.
    from power_sync.inverters.fronius_native import FroniusNativeExportLimit
    controller._native_export = FroniusNativeExportLimit(site.hass, "ps-entry", controller.host,
                                                       controller.port, controller.slave_id)
    controller._request_lock = controller._native_export.request_lock
    return controller, writes


def test_native_export_uses_working_soft_limit_and_restores_original(native_site):
    controller, writes = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert native_site.calls == [{"entity_id": "number.renamed_export", "value": 0}]
    assert controller.last_curtail_mode == "native_export"
    assert controller.last_curtail_limit_w == 0
    assert all(value == 10000 for address, value in writes if address == controller.REG_WMAXLIMPCT)
    assert asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 5000
    assert not any(native_site.saved.values())


def test_native_reload_preserves_original_limit_and_retries_failed_restore(native_site):
    controller, _ = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    controller, _ = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=982, rated_capacity_w=5000))
    native_site.values["number.renamed_export"] = "unavailable"
    assert not asyncio.run(controller.restore())
    assert any(native_site.saved.values())
    native_site.values["number.renamed_export"] = "0"
    assert asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 5000


@pytest.mark.parametrize("value", ["unavailable", "unknown", "nan", "inf", "-1"])
def test_native_invalid_limit_never_writes_or_falls_back(native_site, value):
    native_site.values["number.renamed_export"] = value
    controller, writes = _native_controller(native_site)
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not native_site.calls
    assert not writes


@pytest.mark.parametrize("missing", ["other_host", "no_native_entity"])
def test_native_missing_surface_preserves_legacy_ac_path(native_site, missing):
    if missing == "other_host":
        native_site.entries[0].data["host"] = "192.0.2.2"
    else:
        native_site.rows.clear()
    controller, _ = _native_controller(native_site)
    legacy, writes = _controller_with_control_readback(enabled=1, limit_value=2258)
    legacy._native_export = controller._native_export
    assert asyncio.run(legacy.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert (legacy.REG_WMAXLIMPCT, 2258) in writes
    assert legacy.last_curtail_mode == "load_following"
    assert not native_site.calls


def test_native_store_failure_prevents_any_device_write(native_site, monkeypatch):
    controller, writes = _native_controller(native_site)
    async def failed(value):
        raise OSError("disk full")
    monkeypatch.setattr(controller._native_export._store, "async_save", failed)
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not native_site.calls
    assert not writes


def test_native_failed_ac_cleanup_keeps_zero_export_and_restore_snapshot(native_site):
    controller, writes = _native_controller(native_site)
    async def failed(**kwargs):
        return False
    controller._verify_power_limit_control = failed
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert native_site.values["number.renamed_export"] == "0"
    assert not asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 0
    assert any(native_site.saved.values())


@pytest.mark.parametrize("variant", ["duplicate_entry", "unloaded", "duplicate_entity", "disabled", "wrong_units", "out_of_range"])
def test_native_ambiguous_or_invalid_surface_is_fail_closed(native_site, variant):
    from copy import copy
    if variant == "duplicate_entry":
        other = copy(native_site.entries[0])
        other.entry_id = "other"
        native_site.entries.append(other)
    elif variant == "unloaded":
        native_site.entries[0].state.value = "setup_error"
    elif variant == "duplicate_entity":
        native_site.rows.append(copy(native_site.rows[0]))
    elif variant == "disabled":
        native_site.rows[0].disabled_by = "user"
    elif variant == "wrong_units":
        get = native_site.hass.states.get
        def state(eid):
            result = get(eid)
            result.attributes["unit_of_measurement"] = "kW"
            return result
        native_site.hass.states.get = state
    else:
        native_site.values["number.renamed_export"] = "10001"
    controller, writes = _native_controller(native_site)
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not native_site.calls
    assert not writes


def test_native_session_never_falls_back_if_upstream_disappears(native_site):
    controller, _ = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    native_site.entries.clear()
    controller, writes = _native_controller(native_site)
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not asyncio.run(controller.restore())
    assert not writes
    assert any(native_site.saved.values())


def test_native_does_not_rewrite_zero_every_fast_cycle(native_site):
    controller, _ = _native_controller(native_site)
    for load in (1129, 982, 963):
        assert asyncio.run(controller.curtail(home_load_w=load, rated_capacity_w=5000))
    assert len(native_site.calls) == 1
    assert asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 5000


def test_native_two_powersync_entries_cannot_restore_each_other(native_site):
    from power_sync.inverters.fronius_native import FroniusNativeExportLimit
    owner, _ = _native_controller(native_site)
    other, writes = _native_controller(native_site)
    other._native_export = FroniusNativeExportLimit(native_site.hass, "other-entry", owner.host, 502, 1)
    assert owner._request_lock is other._native_export.request_lock
    async def overlap():
        return await asyncio.gather(
            owner.curtail(home_load_w=1129, rated_capacity_w=5000),
            other.curtail(home_load_w=982, rated_capacity_w=5000),
        )
    assert asyncio.run(overlap()) == [True, False]
    assert not asyncio.run(other.restore())
    assert not writes
    assert asyncio.run(owner.restore())
    assert native_site.calls[-1]["value"] == 5000


def test_native_service_failure_retains_snapshot_and_never_reports_success(native_site):
    controller, writes = _native_controller(native_site)
    async def failed(*args, **kwargs):
        raise RuntimeError("auth failure")
    native_site.hass.services.async_call = failed
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not writes
    assert any(native_site.saved.values())
    assert controller.last_curtail_mode is None


def test_native_service_success_without_updated_entity_is_not_confirmed(native_site):
    controller, writes = _native_controller(native_site)
    async def ignored(*args, **kwargs):
        return None
    native_site.hass.services.async_call = ignored
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not writes
    assert any(native_site.saved.values())


def test_native_factory_binds_hass_and_powersync_entry(native_site):
    import ast
    from typing import Optional
    source = (COMPONENT_ROOT / "inverters" / "__init__.py").read_text()
    function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "get_inverter_controller")
    namespace = {"Optional": Optional, "InverterController": object,
                 "__package__": "power_sync.inverters"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "factory", "exec"), namespace)
    controller = namespace["get_inverter_controller"](
        "fronius", "192.0.2.1", hass=native_site.hass, entry_id="ps-entry")
    assert controller._native_export is not None
    assert controller._request_lock is controller._native_export.request_lock


def test_native_original_zero_is_preserved(native_site):
    native_site.values["number.renamed_export"] = "0"
    controller, _ = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 0


def test_native_service_restore_failure_keeps_exact_retry_value(native_site):
    controller, _ = _native_controller(native_site)
    assert asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    service = native_site.hass.services.async_call
    async def failed(*args, **kwargs):
        raise RuntimeError("offline")
    native_site.hass.services.async_call = failed
    assert not asyncio.run(controller.restore())
    assert native_site.values["number.renamed_export"] == "0"
    assert any(native_site.saved.values())
    native_site.hass.services.async_call = service
    assert asyncio.run(controller.restore())
    assert native_site.calls[-1]["value"] == 5000


@pytest.mark.parametrize("snapshot", [
    {"unexpected": True},
    {"entity_id": "number.renamed_export", "upstream_entry_id": "upstream",
     "limit_w": "nan", "owner_entry_id": "ps-entry"},
    {"entity_id": "number.renamed_export", "upstream_entry_id": "upstream",
     "limit_w": 5000, "owner_entry_id": "foreign-owner"},
])
def test_native_invalid_persisted_ownership_never_writes(native_site, snapshot):
    controller, writes = _native_controller(native_site)
    native_site.saved[controller._native_export._store.key] = snapshot
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not asyncio.run(controller.restore())
    assert not native_site.calls
    assert not writes


def test_native_unload_during_snapshot_save_prevents_device_write(native_site):
    native_site.hass.data = {"power_sync": {"ps-entry": {"aemo_dispatch_generation": object()}}}
    controller, writes = _native_controller(native_site)
    save = controller._native_export._store.async_save
    async def unload(value):
        await save(value)
        native_site.hass.data["power_sync"].pop("ps-entry", None)
    controller._native_export._store.async_save = unload
    assert not asyncio.run(controller.curtail(home_load_w=1129, rated_capacity_w=5000))
    assert not native_site.calls
    assert not writes
    assert any(native_site.saved.values())
