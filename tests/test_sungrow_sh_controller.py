"""Regression tests for Sungrow SH Modbus force-mode writes."""

from __future__ import annotations

import asyncio
from datetime import datetime
import importlib
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _install_package_stubs() -> None:
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


def _install_const_stub() -> None:
    const = types.ModuleType("power_sync.const")
    const.DOMAIN = "power_sync"
    const.UPDATE_INTERVAL_PRICES = 300
    const.UPDATE_INTERVAL_ENERGY = 30
    const.AMBER_API_BASE_URL = "https://example.test"
    const.TESLEMETRY_API_BASE_URL = "https://example.test"
    const.FLEET_API_BASE_URL = "https://example.test"
    const.POWERSYNC_API_BASE_URL = "https://example.test"
    const.POWERSYNC_AUTH_ME_URL = "https://example.test/auth/me"
    const.TESLA_PROVIDER_TESLEMETRY = "teslemetry"
    const.TESLA_PROVIDER_FLEET_API = "fleet_api"
    const.TESLA_PROVIDER_POWERSYNC = "powersync"
    const.POWER_SYNC_USER_AGENT = "PowerSync/test"
    const.DEFAULT_SOLCAST_ESTIMATE_TYPE = "estimate"
    const.SOLCAST_ESTIMATE = "estimate"
    const.SOLCAST_ESTIMATE10 = "estimate10"
    const.SOLCAST_ESTIMATE90 = "estimate90"
    const.DEFAULT_TWAP_WINDOW_DAYS = 7
    const.MIN_TWAP_SAMPLES = 1
    const.FLOW_POWER_MARKET_AVG = "market_avg"
    const.FLOW_POWER_KWATCH_REGIONS = {}
    const.CONF_FLEET_API_BASE_URL = "fleet_api_base_url"
    const.CONF_MONITORING_MODE = "monitoring_mode"
    const.CONF_POWERSYNC_CLIENT_INSTANCE_ID = "powersync_client_instance_id"
    const.CONF_EV_PROVIDER = "ev_provider"
    const.CONF_TESLA_BLE_ENTITY_PREFIX = "tesla_ble_entity_prefix"
    const.CONF_TESLA_BLE_VEHICLE_MAPPING = "tesla_ble_vehicle_mapping"
    const.DEFAULT_TESLA_BLE_ENTITY_PREFIX = "tesla_ble"
    const.EV_PROVIDER_BOTH = "both"
    const.EV_PROVIDER_TESLA_BLE = "tesla_ble"
    const.TESLA_SITE_INFO_CACHE_TTL_SECONDS = 3600
    const.CONF_SIGENERGY_CHARGER_ENABLED = "sigenergy_charger_enabled"
    const.CONF_SIGENERGY_CHARGER_HOST = "sigenergy_charger_host"
    const.CONF_SIGENERGY_CHARGER_PORT = "sigenergy_charger_port"
    const.CONF_SIGENERGY_CHARGER_SLAVE_ID = "sigenergy_charger_slave_id"
    const.CONF_SIGENERGY_CHARGER_TYPE = "sigenergy_charger_type"
    const.CONF_SIGENERGY_MODBUS_HOST = "sigenergy_modbus_host"
    const.DEFAULT_SIGENERGY_CHARGER_PORT = 502
    const.DEFAULT_SIGENERGY_CHARGER_SLAVE_ID = 1
    const.SIGENERGY_CHARGER_EVAC = "evac"
    const.SIGENERGY_CHARGER_EVDC = "evdc"
    sys.modules["power_sync.const"] = const


def _install_homeassistant_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_aiohttp_client = types.ModuleType("homeassistant.helpers.aiohttp_client")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_update_coordinator.DataUpdateCoordinator = type("DataUpdateCoordinator", (), {})
    ha_update_coordinator.UpdateFailed = type("UpdateFailed", (Exception,), {})
    ha_aiohttp_client.async_get_clientsession = lambda hass: None
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_storage.Store = type("Store", (), {})
    ha_dt.utcnow = lambda: datetime(2026, 5, 20, 0, 0, 0)
    ha_dt.now = lambda: datetime(2026, 5, 20, 10, 0, 0)

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update_coordinator
    sys.modules["homeassistant.helpers.aiohttp_client"] = ha_aiohttp_client
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt


def _install_pymodbus_stub_if_missing() -> None:
    if importlib.util.find_spec("pymodbus") is not None:
        return

    pymodbus = types.ModuleType("pymodbus")
    pymodbus.__version__ = "3.8.6"
    pymodbus_client = types.ModuleType("pymodbus.client")
    pymodbus_exceptions = types.ModuleType("pymodbus.exceptions")

    class AsyncModbusTcpClient:
        pass

    pymodbus_client.AsyncModbusTcpClient = AsyncModbusTcpClient
    pymodbus_exceptions.ModbusException = type("ModbusException", (Exception,), {})

    sys.modules["pymodbus"] = pymodbus
    sys.modules["pymodbus.client"] = pymodbus_client
    sys.modules["pymodbus.exceptions"] = pymodbus_exceptions


_install_package_stubs()
_install_pymodbus_stub_if_missing()

from power_sync.inverters.sungrow_sh import SungrowSHController  # noqa: E402


def _load_sungrow_energy_coordinator():
    module_names = (
        "homeassistant",
        "homeassistant.core",
        "homeassistant.exceptions",
        "homeassistant.helpers",
        "homeassistant.helpers.update_coordinator",
        "homeassistant.helpers.aiohttp_client",
        "homeassistant.helpers.dispatcher",
        "homeassistant.helpers.storage",
        "homeassistant.util",
        "homeassistant.util.dt",
        "power_sync.const",
        "power_sync.coordinator",
    )
    saved_modules = {name: sys.modules.get(name) for name in module_names}
    _install_homeassistant_stubs()
    _install_const_stub()
    sys.modules.pop("power_sync.coordinator", None)
    coordinator_module = importlib.import_module("power_sync.coordinator")

    def restore() -> None:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return coordinator_module.SungrowEnergyCoordinator, restore


def _load_dual_sungrow_coordinator():
    """Load the aggregate coordinator alongside the existing HA stubs."""
    _sungrow, restore = _load_sungrow_energy_coordinator()
    return sys.modules["power_sync.coordinator"].DualSungrowCoordinator, restore


def _controller_with_recorded_writes():
    controller = SungrowSHController("192.0.2.10")
    writes: list[tuple[int, int]] = []
    ems_reads = [
        [controller.EMS_SELF_CONSUMPTION],
        [controller.EMS_FORCED],
    ]

    async def connect() -> bool:
        return True

    async def write_register(address: int, value: int) -> bool:
        writes.append((address, value))
        return True

    async def read_register(address: int, count: int = 1):
        if address == controller.REG_EXPORT_LIMIT_ENABLED:
            return [controller.EXPORT_LIMIT_DISABLE]
        if address == controller.REG_EXPORT_LIMIT_SETTING:
            return [0]
        if address == controller.REG_EMS_MODE:
            return ems_reads.pop(0)
        return None

    controller.connect = connect
    controller._write_register = write_register
    controller._read_register = read_register
    return controller, writes


@pytest.mark.parametrize("outcome", ["failed", "timeout", "exception"])
def test_failed_connections_close_clients_and_disable_background_retries(
    monkeypatch, outcome,
):
    """Every failed poll must discard its client before the next attempt."""
    clients = []

    class FailedClient:
        def __init__(self, **kwargs):
            self.connected = False
            self.kwargs = kwargs
            self.close_calls = 0
            clients.append(self)

        async def connect(self):
            if outcome == "failed":
                return False
            if outcome == "exception":
                raise OSError("endpoint unavailable")
            await asyncio.Event().wait()

        def close(self):
            self.close_calls += 1

    sungrow_module = sys.modules[SungrowSHController.__module__]
    monkeypatch.setattr(sungrow_module, "AsyncModbusTcpClient", FailedClient)

    async def run_connects():
        controller = SungrowSHController("192.0.2.10")
        controller.CONNECT_TIMEOUT_SECONDS = 0.01
        results = [await controller.connect(), await controller.connect()]
        return controller, results

    controller, results = asyncio.run(run_connects())

    assert results == [False, False]
    assert len(clients) == 2
    assert [client.close_calls for client in clients] == [1, 1]
    assert all(client.kwargs["retries"] == 1 for client in clients)
    assert all(client.kwargs["reconnect_delay"] == 0 for client in clients)
    assert controller._client is None
    assert controller.is_connected is False


def test_force_discharge_writes_requested_power_to_forced_power_register():
    async def run_force_discharge():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.force_discharge(power_w=20000)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_force_discharge())

    assert result
    assert writes == [
        (controller.REG_CHARGE_DISCHARGE_POWER, 20000),
        (controller.REG_EMS_MODE, controller.EMS_FORCED),
        (controller.REG_CHARGE_CMD, controller.CMD_DISCHARGE),
    ]


def test_restore_normal_rejects_unsupported_ems_fallback_when_stop_fails():
    async def run_restore():
        controller = SungrowSHController("192.0.2.10")
        writes: list[tuple[int, int]] = []

        async def connect() -> bool:
            return True

        async def write_register(address: int, value: int) -> bool:
            writes.append((address, value))
            if address in (controller.REG_CHARGE_CMD, controller.REG_EMS_MODE):
                return False
            return True

        controller.connect = connect
        controller._write_register = write_register
        controller._ems_registers_supported = False

        result = await controller.restore_normal()
        return result, writes, controller

    result, writes, controller = asyncio.run(run_restore())

    assert result is False
    assert (controller.REG_CHARGE_CMD, controller.CMD_STOP) in writes


def test_force_discharge_disables_stale_zero_export_limit_first():
    async def run_force_discharge():
        controller = SungrowSHController("192.0.2.10")
        writes: list[tuple[int, int]] = []
        ems_reads = [
            [controller.EMS_SELF_CONSUMPTION],
            [controller.EMS_FORCED],
        ]

        async def connect() -> bool:
            return True

        async def write_register(address: int, value: int) -> bool:
            writes.append((address, value))
            return True

        async def read_register(address: int, count: int = 1):
            if address == controller.REG_EXPORT_LIMIT_ENABLED:
                return [controller.EXPORT_LIMIT_ENABLE]
            if address == controller.REG_EXPORT_LIMIT_SETTING:
                return [0]
            if address == controller.REG_EMS_MODE:
                return ems_reads.pop(0)
            return None

        controller.connect = connect
        controller._write_register = write_register
        controller._read_register = read_register

        result = await controller.force_discharge(power_w=15000)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_force_discharge())

    assert result
    assert writes == [
        (controller.REG_EXPORT_LIMIT_ENABLED, controller.EXPORT_LIMIT_DISABLE),
        (controller.REG_CHARGE_DISCHARGE_POWER, 15000),
        (controller.REG_EMS_MODE, controller.EMS_FORCED),
        (controller.REG_CHARGE_CMD, controller.CMD_DISCHARGE),
    ]


def test_force_charge_writes_requested_power_to_forced_power_register():
    async def run_force_charge():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.force_charge(power_w=12000)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_force_charge())

    assert result
    assert writes == [
        (controller.REG_CHARGE_DISCHARGE_POWER, 12000),
        (controller.REG_EMS_MODE, controller.EMS_FORCED),
        (controller.REG_CHARGE_CMD, controller.CMD_CHARGE),
    ]


def test_force_charge_clamps_below_practical_minimum():
    async def run_force_charge():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.force_charge(power_w=89)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_force_charge())

    assert result
    assert writes == [
        (controller.REG_CHARGE_DISCHARGE_POWER, controller.MIN_FORCED_POWER_W),
        (controller.REG_EMS_MODE, controller.EMS_FORCED),
        (controller.REG_CHARGE_CMD, controller.CMD_CHARGE),
    ]


def test_force_discharge_clamps_below_practical_minimum():
    async def run_force_discharge():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.force_discharge(power_w=150)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_force_discharge())

    assert result
    assert writes == [
        (controller.REG_CHARGE_DISCHARGE_POWER, controller.MIN_FORCED_POWER_W),
        (controller.REG_EMS_MODE, controller.EMS_FORCED),
        (controller.REG_CHARGE_CMD, controller.CMD_DISCHARGE),
    ]


def test_set_export_limit_clamps_enabled_zero_limit_to_winet_floor():
    async def run_set_export_limit():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.set_export_limit(0)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_set_export_limit())

    assert result
    assert writes == [
        (controller.REG_EXPORT_LIMIT_SETTING, controller.MIN_ENABLED_EXPORT_LIMIT_W),
        (controller.REG_EXPORT_LIMIT_ENABLED, controller.EXPORT_LIMIT_ENABLE),
    ]


def test_set_export_limit_none_disables_without_floor_write():
    async def run_set_export_limit():
        controller, writes = _controller_with_recorded_writes()
        result = await controller.set_export_limit(None)
        return result, controller, writes

    result, controller, writes = asyncio.run(run_set_export_limit())

    assert result
    assert writes == [
        (controller.REG_EXPORT_LIMIT_ENABLED, controller.EXPORT_LIMIT_DISABLE),
    ]


def test_setup_battery_data_reads_only_core_battery_block():
    async def run_read():
        controller = SungrowSHController("192.0.2.10")
        calls: list[tuple[int, int]] = []

        async def connect() -> bool:
            return True

        async def read_input_register(address: int, count: int = 1):
            calls.append((address, count))
            return [5751, 59, 3398, 297, 980, 208, 298]

        controller.connect = connect
        controller._read_input_register = read_input_register
        data = await controller.get_setup_battery_data()
        return data, calls, controller

    data, calls, controller = asyncio.run(run_read())

    assert calls == [(controller.REG_BATTERY_VOLTAGE, 7)]
    assert data["battery_voltage"] == 575.1
    assert data["battery_soc"] == 29.7
    assert data["battery_soh"] == 98.0
    assert data["battery_current"] == 5.9
    assert data["battery_power"] == 3398
    assert data["battery_temp"] == 20.8


def test_battery_data_aborts_poll_when_core_block_times_out():
    async def run_read():
        controller = SungrowSHController("192.0.2.10")
        input_reads: list[tuple[int, int]] = []
        holding_reads: list[tuple[int, int]] = []
        disconnect_calls = 0

        async def connect() -> bool:
            return True

        async def read_input_register(address: int, count: int = 1):
            input_reads.append((address, count))
            return None

        async def read_register(address: int, count: int = 1):
            holding_reads.append((address, count))
            return None

        async def disconnect() -> None:
            nonlocal disconnect_calls
            disconnect_calls += 1

        controller.connect = connect
        controller._read_input_register = read_input_register
        controller._read_register = read_register
        controller.disconnect = disconnect

        data = await controller.get_battery_data()
        return data, input_reads, holding_reads, disconnect_calls, controller

    data, input_reads, holding_reads, disconnect_calls, controller = asyncio.run(
        run_read()
    )

    assert data == {}
    assert input_reads == [(controller.REG_BATTERY_VOLTAGE, 7)]
    assert holding_reads == []
    assert disconnect_calls == 1


def test_battery_data_prefers_mkaiser_telemetry_registers_without_write_probe():
    async def run_read():
        controller = SungrowSHController("192.0.2.10")
        input_reads: list[tuple[int, int]] = []
        holding_reads: list[tuple[int, int]] = []

        async def connect() -> bool:
            return True

        async def read_input_register(address: int, count: int = 1):
            input_reads.append((address, count))
            values = {
                controller.REG_BATTERY_VOLTAGE: [5751, 59, 3398, 297, 980, 208, 298],
                controller.REG_BATTERY_POWER_S32: [3398, 0],
                controller.REG_BATTERY_CURRENT_PRECISE: [123],
                controller.REG_INVERTER_TEMP: [312],
                controller.REG_METER_ACTIVE_POWER: [1500, 0],
                controller.REG_BMS_MAX_CHARGE_CURRENT: [40],
                controller.REG_BMS_MAX_DISCHARGE_CURRENT: [35],
            }
            return values.get(address)

        async def read_register(address: int, count: int = 1):
            holding_reads.append((address, count))
            if address == controller.REG_EMS_MODE:
                return [controller.EMS_SELF_CONSUMPTION]
            if address == controller.REG_MAX_CHARGE_POWER:
                return [1200]
            if address == controller.REG_MAX_DISCHARGE_POWER:
                return [1500]
            if address == controller.REG_EXPORT_LIMIT_ENABLED:
                return [controller.EXPORT_LIMIT_ENABLE]
            if address == controller.REG_BACKUP_RESERVE:
                return [30]
            return None

        async def write_register(address: int, value: int) -> bool:
            raise AssertionError("get_battery_data must not write during telemetry reads")

        controller.connect = connect
        controller._read_input_register = read_input_register
        controller._read_register = read_register
        controller._write_register = write_register

        data = await controller.get_battery_data()
        return data, input_reads, holding_reads, controller

    data, input_reads, holding_reads, controller = asyncio.run(run_read())

    assert data["battery_voltage"] == 575.1
    assert data["battery_soc"] == 29.7
    assert data["battery_power"] == 3398
    assert data["battery_current"] == 12.3
    assert data["inverter_temperature"] == 31.2
    assert data["meter_power"] == 1500
    assert data["charge_rate_limit_kw"] == 12.0
    assert data["discharge_rate_limit_kw"] == 15.0
    assert data["discharge_rate_limit_source"] == "configured_power"
    assert "bms_max_discharge_current_a" not in data
    assert data["export_limit_enabled"] is True
    assert data["backup_reserve"] == 30
    assert (controller.REG_MAX_CHARGE_POWER, 1) in holding_reads
    assert (controller.REG_MAX_DISCHARGE_POWER, 1) in holding_reads
    assert (controller.REG_BMS_MAX_CHARGE_CURRENT, 1) not in input_reads
    assert (13065, 1) not in holding_reads
    assert (13066, 1) not in holding_reads


def test_battery_data_marks_zero_bms_discharge_capability():
    async def run_read():
        controller = SungrowSHController("192.0.2.10")

        async def connect() -> bool:
            return True

        async def read_input_register(address: int, count: int = 1):
            values = {
                controller.REG_BATTERY_VOLTAGE: [5751, 0, 0, 49, 980, 208, 298],
                controller.REG_BATTERY_POWER_S32: [0, 0],
                controller.REG_BATTERY_CURRENT_PRECISE: [0],
                controller.REG_INVERTER_TEMP: [312],
                controller.REG_METER_ACTIVE_POWER: [8540, 0],
                controller.REG_BMS_MAX_CHARGE_CURRENT: [40],
                controller.REG_BMS_MAX_DISCHARGE_CURRENT: [0],
            }
            return values.get(address)

        async def read_register(address: int, count: int = 1):
            return None

        controller.connect = connect
        controller._read_input_register = read_input_register
        controller._read_register = read_register
        return await controller.get_battery_data()

    data = asyncio.run(run_read())

    assert data["battery_soc"] == 4.9
    assert data["bms_max_discharge_current_a"] == 0
    assert data["discharge_rate_limit_kw"] == 0.0
    assert data["discharge_rate_limit_source"] == "bms_current"


def test_sungrow_rate_limits_use_mkaiser_power_registers():
    async def run_limits():
        controller = SungrowSHController("192.0.2.10")
        writes: list[tuple[int, int]] = []

        async def connect() -> bool:
            return True

        async def write_register(address: int, value: int) -> bool:
            writes.append((address, value))
            return True

        controller.connect = connect
        controller._write_register = write_register

        charge_ok = await controller.set_charge_rate_limit(12.0)
        discharge_ok = await controller.set_discharge_rate_limit(15.0)
        return charge_ok, discharge_ok, writes, controller

    charge_ok, discharge_ok, writes, controller = asyncio.run(run_limits())

    assert charge_ok
    assert discharge_ok
    assert writes == [
        (controller.REG_MAX_CHARGE_POWER, 1200),
        (controller.REG_MAX_DISCHARGE_POWER, 1500),
    ]


def test_backup_reserve_uses_mkaiser_whole_percent_register():
    async def run_backup_reserve():
        controller = SungrowSHController("192.0.2.10")
        writes: list[tuple[int, int]] = []
        reads: list[tuple[int, int]] = []

        async def connect() -> bool:
            return True

        async def write_register(address: int, value: int) -> bool:
            writes.append((address, value))
            return True

        async def read_register(address: int, count: int = 1):
            reads.append((address, count))
            return [45]

        controller.connect = connect
        controller._write_register = write_register
        controller._read_register = read_register

        write_ok = await controller.set_backup_reserve(45)
        current = await controller.get_backup_reserve()
        return write_ok, current, writes, reads, controller

    write_ok, current, writes, reads, controller = asyncio.run(run_backup_reserve())

    assert write_ok
    assert current == 45
    assert writes == [(controller.REG_BACKUP_RESERVE, 45)]
    assert reads == [(controller.REG_BACKUP_RESERVE, 1)]


class _FakeSungrowController:
    def __init__(self):
        self.charge_rate_limits: list[float] = []
        self.discharge_rate_limits: list[float] = []
        self.export_limits: list[int | None] = []
        self.force_charge_power_w: list[float] = []
        self.force_discharge_power_w: list[float] = []
        self.restore_normal_calls = 0
        self.idle_mode_calls = 0
        self.battery_data = {
            "charge_rate_limit_kw": 15.0,
            "discharge_rate_limit_kw": 15.0,
            "export_limit_enabled": False,
            "export_limit_w": 0,
        }
        self.fail_zero_discharge_limit = False
        self.fail_discharge_limit = False
        self.fail_discharge_limit_count = 0
        self._rate_limit_writable: bool | None = None
        self.force_discharge_result = True
        self.disconnect_calls = 0

    @property
    def rate_limit_writable(self) -> bool | None:
        return self._rate_limit_writable

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def set_charge_rate_limit(self, kw: float) -> bool:
        self.charge_rate_limits.append(kw)
        return True

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        self.discharge_rate_limits.append(kw)
        if self.fail_discharge_limit_count > 0:
            self.fail_discharge_limit_count -= 1
            self._rate_limit_writable = False
            return False
        if self.fail_discharge_limit or (kw == 0 and self.fail_zero_discharge_limit):
            self._rate_limit_writable = False
            return False
        self._rate_limit_writable = True
        return True

    async def set_export_limit(self, watts: int | None) -> bool:
        self.export_limits.append(watts)
        return True

    async def force_charge(self, power_w: float = 5000) -> bool:
        self.force_charge_power_w.append(power_w)
        return True

    async def force_discharge(self, power_w: float = 5000) -> bool:
        self.force_discharge_power_w.append(power_w)
        return self.force_discharge_result

    async def restore_normal(self) -> bool:
        self.restore_normal_calls += 1
        return True

    async def set_idle_mode(self) -> bool:
        self.idle_mode_calls += 1
        return True

    async def restore_from_idle(self) -> bool:
        self.restore_normal_calls += 1
        return True

    async def get_battery_data(self) -> dict:
        return self.battery_data

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    @property
    def rate_limit_writable(self) -> bool | None:
        return self._rate_limit_writable


class _FakeStore:
    def __init__(self, initial=None, *, fail_save=False):
        self.data = initial
        self.saved: list[dict] = []
        self.fail_save = fail_save

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        if self.fail_save:
            raise OSError("storage unavailable")
        self.data = dict(data)
        self.saved.append(dict(data))


class _FakeEnergyAccumulator:
    _last_update = True

    def __init__(self):
        self.updates: list[tuple[float, float, float, float, object, object]] = []
        self.solar_kwh = 0
        self.grid_import_kwh = 0
        self.grid_export_kwh = 0
        self.battery_charge_kwh = 0
        self.battery_discharge_kwh = 0
        self.load_kwh = 0

    async def async_restore(self) -> None:
        return None

    def update(self, solar_kw, grid_kw, battery_kw, load_kw, buy, sell) -> None:
        self.updates.append((solar_kw, grid_kw, battery_kw, load_kw, buy, sell))

    def as_dict(self) -> dict:
        return {
            "pv_today_kwh": 0,
            "grid_import_today_kwh": 0,
            "grid_export_today_kwh": 0,
            "charge_today_kwh": 0,
            "discharge_today_kwh": 0,
            "load_today_kwh": 0,
            "import_cost_today": 0,
            "export_earnings_today": 0,
        }


def _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller):
    coordinator = SungrowEnergyCoordinator.__new__(SungrowEnergyCoordinator)
    coordinator._controller = fake_controller
    coordinator._telemetry_only = False
    coordinator._modbus_lock = asyncio.Lock()
    coordinator._total_import_baseline = None
    coordinator._total_export_baseline = None
    coordinator._baseline_date = None
    coordinator._ac_inverter_source_id = "sungrow:192.0.2.20:502:1"
    coordinator._ac_inverter_energy_store = None
    coordinator._ac_inverter_daily_energy_restored = False
    coordinator._ac_inverter_daily_energy_kwh = None
    coordinator._ac_inverter_daily_energy_date = None
    coordinator.last_update_success = True
    return coordinator


def test_sungrow_ihomemanager_telemetry_only_blocks_all_native_writes():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class WriteTrap:
        def __getattr__(self, name):
            raise AssertionError(f"telemetry-only path attempted controller access: {name}")

    async def run_checks():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            WriteTrap(),
        )
        coordinator._telemetry_only = True

        results = [
            await coordinator.force_charge(),
            await coordinator.force_discharge(),
            await coordinator.force_grid_export(export_limit_w=5000),
            await coordinator.restore_normal(),
            await coordinator.set_max_soc(90),
            await coordinator.set_backup_reserve(20),
            await coordinator.set_backup_mode(),
            await coordinator.set_no_discharge_mode(),
            await coordinator.restore_no_discharge_mode(),
            await coordinator.async_restore_persisted_export_control(),
            await coordinator.restore_work_mode_from_idle(),
            await coordinator.set_charge_rate_limit(2.5),
            await coordinator.set_discharge_rate_limit(2.5),
            await coordinator.set_export_limit(5000),
        ]

        return results

    try:
        results = asyncio.run(run_checks())
    finally:
        restore()

    assert results == [False] * 14


def test_sungrow_direct_connection_keeps_native_control_enabled():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        assert coordinator._native_control_allowed("test write") is True
    finally:
        restore()


def test_sungrow_pending_optimizer_export_restore_is_ownership_only():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        assert coordinator.pending_optimizer_export_restore is False
        coordinator._pre_control_export_limit_captured = True
        assert coordinator.pending_optimizer_export_restore is True
        coordinator._pre_control_export_limit_captured = False
        assert coordinator.pending_optimizer_export_restore is False
        coordinator._optimizer_restore_retry_pending = True
        assert coordinator.pending_optimizer_export_restore is True
    finally:
        restore()


def test_dual_sungrow_aggregates_pending_optimizer_export_restore():
    DualSungrowCoordinator, restore = _load_dual_sungrow_coordinator()

    class Child:
        pending_optimizer_export_restore = False

    try:
        coordinator = DualSungrowCoordinator.__new__(DualSungrowCoordinator)
        coordinator._coord1 = Child()
        coordinator._coord2 = Child()
        assert coordinator.pending_optimizer_export_restore is False
        coordinator._coord2.pending_optimizer_export_restore = True
        assert coordinator.pending_optimizer_export_restore is True
        coordinator._coord2.pending_optimizer_export_restore = False
        coordinator._coord1.pending_optimizer_export_restore = True
        assert coordinator.pending_optimizer_export_restore is True
    finally:
        restore()


def test_sungrow_startup_control_requires_a_valid_telemetry_snapshot():
    """A retained coordinator must not authorize planning from empty defaults."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator.data = None
        assert coordinator.startup_control_ready() is False

        coordinator.data = {
            "telemetry_ready": True,
            "solar_power": 0.0,
            "grid_power": 0.0,
            "battery_power": 0.0,
            "load_power": 0.0,
            "battery_level": 0.0,
        }
        assert coordinator.startup_control_ready() is True

        coordinator.last_update_success = False
        assert coordinator.startup_control_ready() is False

        coordinator.last_update_success = True
        coordinator.data["battery_level"] = float("nan")
        assert coordinator.startup_control_ready() is False
    finally:
        restore()


def test_dual_sungrow_requires_both_coordinators_to_be_ready():
    """Child and aggregate failures must both block optimizer control."""
    DualSungrowCoordinator, restore = _load_dual_sungrow_coordinator()

    class Child:
        def __init__(self, ready):
            self.ready = ready
            self.data = {
                "telemetry_ready": ready,
                "solar_power": 0.0,
                "grid_power": 0.0,
                "battery_power": 0.0,
                "load_power": 0.0,
                "battery_level": 50.0,
            }

        def startup_control_ready(self):
            return self.ready

    async def run_checks():
        coordinator = DualSungrowCoordinator.__new__(DualSungrowCoordinator)
        coordinator._coord1 = Child(True)
        coordinator._coord2 = Child(False)
        coordinator._soc_cap = 100
        coordinator._cap1 = 10.0
        coordinator._cap2 = 10.0
        coordinator.last_update_success = False

        assert coordinator.startup_control_ready() is False

        coordinator._coord2.ready = True
        first_snapshot = await coordinator._async_update_data()
        assert first_snapshot["telemetry_ready"] is True

        coordinator.data = first_snapshot
        coordinator.last_update_success = True
        assert coordinator.startup_control_ready() is True

        coordinator.last_update_success = False
        assert coordinator.startup_control_ready() is False

    try:
        asyncio.run(run_checks())
    finally:
        restore()


def test_sungrow_failed_poll_marks_cached_telemetry_not_ready():
    """A disconnected poll may preserve values, but never control readiness."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FailedController:
        async def get_battery_data(self):
            return {}

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            FailedController(),
        )
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator.data = {
            "telemetry_ready": True,
            "solar_power": 1.0,
            "grid_power": 0.0,
            "battery_power": -0.5,
            "load_power": 0.5,
            "battery_level": 42.0,
        }
        stale = await coordinator._async_update_data()
        coordinator.data = stale
        return stale, coordinator.startup_control_ready()

    try:
        stale, ready = asyncio.run(run_update())
    finally:
        restore()

    assert stale["battery_level"] == 42.0
    assert stale["telemetry_ready"] is False
    assert ready is False


def test_sungrow_daily_grid_totals_survive_same_day_reload():
    """Persisted lifetime baselines must outrank a polluted accumulator."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class PollutedAccumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data["grid_import_today_kwh"] = 0.20
            data["grid_export_today_kwh"] = 25.14
            return data

    async def run_checks():
        store = _FakeStore(
            {
                "date": "2026-05-20",
                "import_baseline_kwh": 2073.9,
                "export_baseline_kwh": 17315.5,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = PollutedAccumulator()
        coordinator._daily_energy_baseline_store = store
        coordinator._daily_energy_baselines_restored = False
        coordinator._daily_energy_baselines_dirty = False

        await coordinator._async_restore_daily_energy_baselines()
        first = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 2074.0,
                "total_export": 17315.8,
            }
        )
        second = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 2074.1,
                "total_export": 17316.0,
            }
        )
        return first, second

    try:
        first, second = asyncio.run(run_checks())
    finally:
        restore()

    assert first["grid_import_today_kwh"] == pytest.approx(0.1)
    assert first["grid_export_today_kwh"] == pytest.approx(0.3)
    assert second["grid_import_today_kwh"] == pytest.approx(0.2)
    assert second["grid_export_today_kwh"] == pytest.approx(0.5)


def test_sungrow_daily_earnings_reject_partial_accumulator_coverage():
    """Ticket #336: never pair full-day hardware export with partial $0."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class PartialAccumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data.update(
                {
                    "grid_import_today_kwh": 23.58,
                    "grid_export_today_kwh": 0.12,
                    "import_cost_today": 3.84,
                    "export_earnings_today": 0.0,
                    "import_cost_covered_kwh": 23.58,
                    "export_earnings_covered_kwh": 0.12,
                    "import_cost_coverage": "complete",
                    "export_earnings_coverage": "complete",
                }
            )
            return data

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = PartialAccumulator()
        summary = coordinator._build_energy_summary(
            {
                "daily_import": 26.8,
                "daily_export": 11.8,
                "daily_pv_generation": 17.7,
                "daily_battery_charge": 34.4,
                "daily_battery_discharge": 34.8,
            }
        )
    finally:
        restore()

    assert summary["grid_export_today_kwh"] == 11.8
    assert summary["grid_export_today_source"] == "sungrow_daily_register"
    assert summary["export_earnings_today"] is None
    assert summary["export_earnings_coverage"] == "partial"
    assert summary["avg_cost_per_kwh_today"] is None


def test_sungrow_daily_earnings_keep_matching_reward_window_coverage():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class CompleteAccumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data.update(
                {
                    "grid_import_today_kwh": 26.7,
                    "grid_export_today_kwh": 11.72,
                    "import_cost_today": 4.0,
                    "export_earnings_today": 3.0472,
                    "import_cost_covered_kwh": 26.7,
                    "export_earnings_covered_kwh": 11.72,
                    "import_cost_coverage": "complete",
                    "export_earnings_coverage": "complete",
                }
            )
            return data

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = CompleteAccumulator()
        summary = coordinator._build_energy_summary(
            {
                "daily_import": 26.8,
                "daily_export": 11.8,
                "daily_pv_generation": 17.7,
                "daily_battery_charge": 34.4,
                "daily_battery_discharge": 34.8,
            }
        )
    finally:
        restore()

    assert summary["export_earnings_today"] == pytest.approx(3.0472)
    assert summary["export_earnings_coverage"] == "complete"


def test_sungrow_daily_earnings_keep_small_import_over_coverage():
    """Ticket #336: priced >= metered is full coverage, not a coverage gap.

    The reported site had priced 0.26 kWh of import against a 0.10 kWh
    hardware daily register.  The percentage tolerance term is meaningless on
    a counter that small, so the 0.05 kWh absolute floor rejected it and both
    Avg Cost per kWh sensors read Unknown all day, even though every imported
    kWh carried a price.
    """
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class OverCoveredAccumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data.update(
                {
                    "grid_import_today_kwh": 0.26,
                    "grid_export_today_kwh": 22.30,
                    "import_cost_today": 0.0787,
                    "export_earnings_today": 0.2235,
                    "import_cost_covered_kwh": 0.26,
                    "export_earnings_covered_kwh": 22.362,
                    "import_cost_coverage": "complete",
                    "export_earnings_coverage": "complete",
                }
            )
            return data

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = OverCoveredAccumulator()
        summary = coordinator._build_energy_summary(
            {
                "daily_import": 0.10,
                "daily_export": 22.80,
                "daily_pv_generation": 57.20,
                "daily_battery_charge": 29.60,
                "daily_battery_discharge": 6.60,
            }
        )
    finally:
        restore()

    assert summary["grid_import_today_kwh"] == pytest.approx(0.10)
    assert summary["import_cost_today"] == pytest.approx(0.0787)
    assert summary["import_cost_coverage"] == "complete"
    assert summary["export_earnings_today"] == pytest.approx(0.2235)
    assert summary["export_earnings_coverage"] == "complete"
    assert summary["load_today_kwh"] == pytest.approx(11.50)
    assert summary["avg_cost_per_kwh_today"] == pytest.approx(-0.0126)


def test_dual_sungrow_keeps_primary_grid_cost_coverage():
    """The backup-port inverter must not hide primary partial accounting."""
    DualSungrowCoordinator, restore = _load_dual_sungrow_coordinator()

    class Child:
        def __init__(self, data):
            self.data = data

        def startup_control_ready(self):
            return True

    primary = Child(
        {
            "solar_power": 2.0,
            "grid_power": -1.0,
            "battery_power": 1.0,
            "load_power": 2.0,
            "battery_level": 50.0,
            "energy_summary": {
                "pv_today_kwh": 10.0,
                "grid_import_today_kwh": 26.8,
                "grid_export_today_kwh": 11.8,
                "charge_today_kwh": 4.0,
                "discharge_today_kwh": 5.0,
                "load_today_kwh": 20.0,
                "import_cost_today": None,
                "export_earnings_today": None,
                "import_cost_covered_kwh": 23.58,
                "export_earnings_covered_kwh": 0.12,
                "import_cost_coverage": "partial",
                "export_earnings_coverage": "partial",
                "grid_import_today_source": "sungrow_daily_register",
                "grid_export_today_source": "sungrow_daily_register",
                "mtd_import_cost": None,
                "mtd_export_earnings": None,
                "mtd_load_kwh": 20.0,
            },
        }
    )
    secondary = Child(
        {
            "solar_power": 1.0,
            "grid_power": -99.0,
            "battery_power": 0.5,
            "load_power": 1.0,
            "battery_level": 60.0,
            "energy_summary": {
                "pv_today_kwh": 5.0,
                "grid_import_today_kwh": 99.0,
                "grid_export_today_kwh": 99.0,
                "charge_today_kwh": 2.0,
                "discharge_today_kwh": 3.0,
                "load_today_kwh": 10.0,
                "import_cost_today": 9.0,
                "export_earnings_today": 10.0,
                "mtd_import_cost": 9.0,
                "mtd_export_earnings": 10.0,
                "mtd_load_kwh": 10.0,
            },
        }
    )

    async def run_update():
        coordinator = DualSungrowCoordinator.__new__(DualSungrowCoordinator)
        coordinator._coord1 = primary
        coordinator._coord2 = secondary
        coordinator._soc_cap = 100
        coordinator._cap1 = 10.0
        coordinator._cap2 = 10.0
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    summary = data["energy_summary"]
    assert data["grid_power"] == -1.0
    assert summary["grid_export_today_kwh"] == 11.8
    assert summary["export_earnings_today"] is None
    assert summary["export_earnings_coverage"] == "partial"
    assert summary["avg_cost_per_kwh_today"] is None


def test_sungrow_daily_grid_baselines_initialize_and_persist_independently():
    """A late lifetime register must not reset the baseline already available."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_checks():
        store = _FakeStore()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._daily_energy_baseline_store = store
        coordinator._daily_energy_baselines_restored = False
        coordinator._daily_energy_baselines_dirty = False

        await coordinator._async_restore_daily_energy_baselines()
        first = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_export": 500.0,
            }
        )
        await coordinator._async_save_daily_energy_baselines()
        second = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 100.0,
                "total_export": 500.2,
            }
        )
        await coordinator._async_save_daily_energy_baselines()
        return first, second, store.data

    try:
        first, second, stored = asyncio.run(run_checks())
    finally:
        restore()

    assert first["grid_export_today_kwh"] == 0.0
    assert second["grid_import_today_kwh"] == 0.0
    assert second["grid_export_today_kwh"] == pytest.approx(0.2)
    assert stored == {
        "date": "2026-05-20",
        "import_baseline_kwh": 100.0,
        "export_baseline_kwh": 500.0,
    }


def test_sungrow_lifetime_counter_rollback_keeps_accumulator_fallback():
    """A reset lifetime counter must not publish a negative daily total."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class Accumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data["grid_import_today_kwh"] = 0.4
            data["grid_export_today_kwh"] = 0.7
            return data

    try:
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = Accumulator()
        coordinator._baseline_date = "2026-05-20"
        coordinator._total_import_baseline = 100.0
        coordinator._total_export_baseline = 500.0
        rollback = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 99.0,
                "total_export": 499.0,
            }
        )
        invalid = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 0xFFFFFFFF * 0.1,
                "total_export": 0xFFFFFFFF * 0.1,
            }
        )
        assert coordinator._valid_daily_total("unavailable") is None
        assert coordinator._valid_daily_total(float("nan")) is None
    finally:
        restore()

    assert rollback["grid_import_today_kwh"] == 0.4
    assert rollback["grid_export_today_kwh"] == 0.7
    assert invalid["grid_import_today_kwh"] == 0.4
    assert invalid["grid_export_today_kwh"] == 0.7


def test_sungrow_daily_grid_baseline_load_failure_retries_without_overwrite():
    """A transient Store load failure must not replace a valid baseline."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class Accumulator(_FakeEnergyAccumulator):
        def as_dict(self):
            data = super().as_dict()
            data["grid_import_today_kwh"] = 0.4
            data["grid_export_today_kwh"] = 0.7
            return data

    class FlakyStore(_FakeStore):
        def __init__(self):
            super().__init__(
                {
                    "date": "2026-05-20",
                    "import_baseline_kwh": 100.0,
                    "export_baseline_kwh": 500.0,
                }
            )
            self.load_calls = 0
            self.save_calls = 0

        async def async_load(self):
            self.load_calls += 1
            if self.load_calls == 1:
                raise OSError("temporary Store read failure")
            return self.data

        async def async_save(self, data):
            self.save_calls += 1
            await super().async_save(data)

    async def run_checks():
        store = FlakyStore()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            object(),
        )
        coordinator._energy_acc = Accumulator()
        coordinator._daily_energy_baseline_store = store
        coordinator._daily_energy_baselines_restored = False
        coordinator._daily_energy_baselines_dirty = False

        await coordinator._async_restore_daily_energy_baselines()
        fallback = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 100.4,
                "total_export": 500.7,
            }
        )
        await coordinator._async_save_daily_energy_baselines()

        await coordinator._async_restore_daily_energy_baselines()
        restored = coordinator._build_energy_summary(
            {
                "daily_import": 0.0,
                "daily_export": 0.0,
                "total_import": 100.5,
                "total_export": 500.8,
            }
        )
        return fallback, restored, store

    try:
        fallback, restored, store = asyncio.run(run_checks())
    finally:
        restore()

    assert fallback["grid_import_today_kwh"] == 0.4
    assert fallback["grid_export_today_kwh"] == 0.7
    assert restored["grid_import_today_kwh"] == 0.5
    assert restored["grid_export_today_kwh"] == 0.8
    assert store.load_calls == 2
    assert store.save_calls == 0


def test_sungrow_successful_retry_rechecks_persisted_export_recovery_once():
    """The first recovered snapshot must finish interrupted export cleanup."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class RecoveredController:
        async def get_battery_data(self):
            return {
                "battery_soc": 42.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 0,
            }

    async def run_updates():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            RecoveredController(),
        )
        coordinator.hass = types.SimpleNamespace(data={"power_sync": {"entry-1": {}}})
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._persisted_export_control_recovery_pending = True
        recovery_results = iter((False, True))
        recovery_calls = 0

        async def recover():
            nonlocal recovery_calls
            recovery_calls += 1
            return next(recovery_results)

        coordinator.async_restore_persisted_export_control = recover
        first = await coordinator._async_update_data()
        second = await coordinator._async_update_data()
        third = await coordinator._async_update_data()
        return first, second, third, recovery_calls

    try:
        first, second, third, recovery_calls = asyncio.run(run_updates())
    finally:
        restore()

    assert first["telemetry_ready"] is False
    assert second["telemetry_ready"] is True
    assert third["telemetry_ready"] is True
    assert recovery_calls == 2


def test_sungrow_monitoring_mode_defers_persisted_export_recovery_writes():
    """Monitoring Mode must publish telemetry without replaying control writes."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class RecoveredController:
        async def get_battery_data(self):
            return {
                "battery_soc": 42.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 0,
            }

    async def run_updates():
        entry = types.SimpleNamespace(
            data={},
            options={"monitoring_mode": True},
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            RecoveredController(),
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}},
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda entry_id: entry,
            ),
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._persisted_export_control_recovery_pending = True
        recovery_calls = 0

        async def recover():
            nonlocal recovery_calls
            recovery_calls += 1
            return True

        coordinator.async_restore_persisted_export_control = recover
        monitoring_snapshot = await coordinator._async_update_data()
        pending_while_monitoring = (
            coordinator._persisted_export_control_recovery_pending
        )
        entry.options["monitoring_mode"] = False
        active_snapshot = await coordinator._async_update_data()
        return (
            monitoring_snapshot,
            pending_while_monitoring,
            active_snapshot,
            coordinator._persisted_export_control_recovery_pending,
            recovery_calls,
        )

    try:
        (
            monitoring_snapshot,
            pending_while_monitoring,
            active_snapshot,
            pending_after_control_enabled,
            recovery_calls,
        ) = asyncio.run(run_updates())
    finally:
        restore()

    assert monitoring_snapshot["telemetry_ready"] is True
    assert pending_while_monitoring is True
    assert active_snapshot["telemetry_ready"] is True
    assert pending_after_control_enabled is False
    assert recovery_calls == 1


def test_sungrow_coordinator_combines_separate_ac_inverter_solar_telemetry():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 27.2,
                "battery_power": -5699,
                "meter_power": 9,
                "load_power": 0,
                "pv_power": 4550,
                "battery_soh": 98.0,
                "battery_temp": 20.8,
                "inverter_temperature": 31.2,
                "daily_pv_generation": 27.9,
                "daily_import": 10.0,
                "daily_export": 17.0,
                "daily_battery_discharge": 20.0,
                "daily_battery_charge": 5.0,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "sungrow",
                            "inverter_source_id": "sungrow:192.0.2.20:502:1",
                            "power_output_w": 3119,
                            "daily_pv_generation": 18.2,
                            "daily_pv_generation_date": "2026-05-20",
                            "last_poll": "2026-05-20T10:00:00",
                        },
                    },
                },
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        data = await coordinator._async_update_data()
        return data, coordinator._energy_acc

    try:
        data, energy_acc = asyncio.run(run_update())
    finally:
        restore()

    assert data["battery_inverter_solar_power"] == 4.55
    assert data["ac_inverter_solar_power"] == 3.119
    assert round(data["solar_power"], 3) == 7.669
    assert data["energy_summary"]["pv_today_kwh"] == 46.1
    assert data["energy_summary"]["load_today_kwh"] == 54.1
    assert data["battery_temp"] == 20.8
    assert data["inverter_temperature"] == 31.2
    assert round(data["load_power"], 3) == 1.979
    assert round(energy_acc.updates[-1][0], 3) == 7.669
    assert round(energy_acc.updates[-1][3], 3) == 1.979


def test_sungrow_coordinator_ignores_stale_ac_power_but_keeps_today_energy():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": -500,
                "load_power": 4050,
                "pv_power": 4550,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "sungrow",
                            "inverter_source_id": "sungrow:192.0.2.20:502:1",
                            "power_output_w": 3119,
                            "daily_pv_generation": 18.2,
                            "daily_pv_generation_date": "2026-05-20",
                            "last_poll": "2026-05-20T09:55:00",
                        },
                    },
                }
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["battery_inverter_solar_power"] == 4.55
    assert data["ac_inverter_solar_power"] == 0
    assert data["solar_power"] == 4.55
    assert data["energy_summary"]["pv_today_kwh"] == 46.1


def test_sungrow_coordinator_drops_previous_day_ac_energy():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 0,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "sungrow",
                            "inverter_source_id": "sungrow:192.0.2.20:502:1",
                            "daily_pv_generation": 18.2,
                            "daily_pv_generation_date": "2026-05-19",
                            "last_poll": "2026-05-19T17:00:00",
                        },
                    },
                }
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["energy_summary"]["pv_today_kwh"] == 0


def test_sungrow_coordinator_restores_ac_daily_energy_before_first_poll():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}}
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._ac_inverter_energy_store = _FakeStore(
            {
                "source_id": "sungrow:192.0.2.20:502:1",
                "date": "2026-05-20",
                "daily_pv_generation": 18.2,
            }
        )
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["ac_inverter_solar_power"] == 0
    assert data["energy_summary"]["pv_today_kwh"] == 46.1


def test_sungrow_coordinator_ignores_persisted_energy_from_old_ac_source():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}}
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._ac_inverter_energy_store = _FakeStore(
            {
                "source_id": "sungrow:192.0.2.99:502:1",
                "date": "2026-05-20",
                "daily_pv_generation": 18.2,
            }
        )
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["energy_summary"]["pv_today_kwh"] == 27.9


def test_sungrow_coordinator_does_not_decrease_same_day_persisted_ac_energy():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "sungrow",
                            "inverter_source_id": "sungrow:192.0.2.20:502:1",
                            "daily_pv_generation": 0,
                            "daily_pv_generation_date": "2026-05-20",
                            "last_poll": "2026-05-20T10:00:00",
                        },
                    },
                }
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._ac_inverter_energy_store = _FakeStore(
            {
                "source_id": "sungrow:192.0.2.20:502:1",
                "date": "2026-05-20",
                "daily_pv_generation": 18.2,
            }
        )
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["energy_summary"]["pv_today_kwh"] == 46.1


def test_sungrow_coordinator_does_not_double_count_ac_in_energy_balance_fallback():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 500,
                "meter_power": -1000,
                "load_power": 7000,
                "pv_power": None,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "sungrow",
                            "inverter_source_id": "sungrow:192.0.2.20:502:1",
                            "power_output_w": 3000,
                            "daily_pv_generation": 18.2,
                            "daily_pv_generation_date": "2026-05-20",
                            "last_poll": "2026-05-20T10:00:00",
                        },
                    },
                }
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["solar_power"] == 7.5
    assert data["battery_inverter_solar_power"] == 4.5
    assert data["ac_inverter_solar_power"] == 3.0
    assert data["load_power"] == 7.0


def test_sungrow_coordinator_does_not_aggregate_other_ac_inverter_brands():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 70.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 4550,
                "pv_power": 4550,
                "daily_pv_generation": 27.9,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "brand": "goodwe",
                            "power_output_w": 3119,
                            "daily_pv_generation": 18.2,
                            "daily_pv_generation_date": "2026-05-20",
                            "last_poll": "2026-05-20T10:00:00",
                        },
                    },
                }
            }
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        return await coordinator._async_update_data()

    try:
        data = asyncio.run(run_update())
    finally:
        restore()

    assert data["solar_power"] == 4.55
    assert data["ac_inverter_solar_power"] == 0
    assert data["energy_summary"]["pv_today_kwh"] == 27.9


def test_sungrow_coordinator_derives_zero_load_register_from_energy_balance():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 100.0,
                "battery_power": 0,
                "meter_power": -511,
                "load_power": 0,
                "pv_power": 727,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}},
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        data = await coordinator._async_update_data()
        return data, coordinator._energy_acc

    try:
        data, energy_acc = asyncio.run(run_update())
    finally:
        restore()

    assert data["solar_power"] == 0.727
    assert data["grid_power"] == -0.511
    assert data["battery_power"] == 0
    assert round(data["load_power"], 3) == 0.216
    assert round(energy_acc.updates[-1][3], 3) == 0.216


def test_sungrow_coordinator_filters_forced_discharge_pv_alias_at_night():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 93.0,
                "battery_power": 9955,
                "meter_power": -9504,
                "load_power": 10265,
                "pv_power": 9814,
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}},
            states=types.SimpleNamespace(
                get=lambda entity_id: types.SimpleNamespace(state="below_horizon")
                if entity_id == "sun.sun"
                else None,
            ),
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        data = await coordinator._async_update_data()
        return data, coordinator._energy_acc

    try:
        data, energy_acc = asyncio.run(run_update())
    finally:
        restore()

    assert data["solar_power"] == 0
    assert data["grid_power"] == -9.504
    assert data["battery_power"] == 9.955
    assert round(data["load_power"], 3) == 0.451
    assert energy_acc.updates[-1][0] == 0
    assert round(energy_acc.updates[-1][3], 3) == 0.451


def test_sungrow_coordinator_filters_low_power_self_consumption_pv_alias_at_night():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FakeController:
        async def get_battery_data(self):
            return {
                "battery_soc": 67.0,
                "battery_power": 819,
                "meter_power": 0,
                "load_power": 1638,
                "pv_power": 819,
                "ems_mode_name": "Normal",
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator, FakeController()
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}},
            states=types.SimpleNamespace(
                get=lambda entity_id: types.SimpleNamespace(state="below_horizon")
                if entity_id == "sun.sun"
                else None,
            ),
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        data = await coordinator._async_update_data()
        return data, coordinator._energy_acc

    try:
        data, energy_acc = asyncio.run(run_update())
    finally:
        restore()

    assert data["solar_power"] == 0
    assert data["grid_power"] == 0
    assert data["battery_power"] == pytest.approx(0.819)
    assert data["load_power"] == pytest.approx(0.819)
    assert energy_acc.updates[-1][0] == 0
    assert energy_acc.updates[-1][3] == pytest.approx(0.819)


def test_sungrow_coordinator_passes_requested_discharge_power_to_controller():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_discharge():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)

        result = await coordinator.force_discharge(duration_minutes=30, power_w=20000)
        return result, fake_controller

    try:
        result, fake_controller = asyncio.run(run_force_discharge())
    finally:
        restore()

    assert result
    assert fake_controller.discharge_rate_limits == []
    assert fake_controller.force_discharge_power_w == [20000]


def test_sungrow_coordinator_passes_requested_charge_power_to_controller():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_charge():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)

        result = await coordinator.force_charge(duration_minutes=30, power_w=12000)
        return result, fake_controller

    try:
        result, fake_controller = asyncio.run(run_force_charge())
    finally:
        restore()

    assert result
    assert fake_controller.charge_rate_limits == []
    assert fake_controller.force_charge_power_w == [12000]


def test_sungrow_no_discharge_restore_reinstates_previous_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_no_discharge_cycle():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        block_result = await coordinator.set_no_discharge_mode()
        restore_result = await coordinator.restore_no_discharge_mode()
        return block_result, restore_result, fake_controller

    try:
        block_result, restore_result, fake_controller = asyncio.run(run_no_discharge_cycle())
    finally:
        restore()

    assert block_result
    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [0, 15.0]


def test_sungrow_idle_hold_uses_discharge_cap_and_allows_charge():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_idle_cycle():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        idle_result = await coordinator.set_backup_mode()
        restore_result = await coordinator.restore_work_mode_from_idle()
        return idle_result, restore_result, fake_controller

    try:
        idle_result, restore_result, fake_controller = asyncio.run(run_idle_cycle())
    finally:
        restore()

    assert idle_result
    assert restore_result
    assert fake_controller.idle_mode_calls == 0
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [0, 15.0]


def test_sungrow_no_discharge_falls_back_to_ten_watts_when_zero_rejected():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_no_discharge_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.fail_zero_discharge_limit = True
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        block_result = await coordinator.set_no_discharge_mode()
        restore_result = await coordinator.restore_no_discharge_mode()
        return block_result, restore_result, fake_controller

    try:
        block_result, restore_result, fake_controller = asyncio.run(run_no_discharge_cycle())
    finally:
        restore()

    assert block_result
    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [0, 0.01, 15.0]


def test_sungrow_restore_repairs_stale_ten_watt_discharge_cap_without_capture():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 0.01,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 0.01,
            "battery_max_charge_power": 10.6,
            "battery_max_discharge_power": 0.01,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [10.6]


def test_sungrow_restore_uses_configured_limit_for_stale_discharge_cap():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 17.54,
            "discharge_rate_limit_kw": 0.01,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "charge_rate_limit_kw": 17.54,
            "discharge_rate_limit_kw": 0.01,
            "battery_max_charge_power": 17.54,
            "battery_max_discharge_power": 0.01,
        }
        entry = types.SimpleNamespace(
            data={"optimization_max_discharge_w": 9200},
            options={},
        )
        coordinator.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda entry_id: entry,
            ),
        )
        coordinator._entry_id = "entry-1"

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [9.2]


def test_sungrow_captured_restore_clamps_to_configured_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 17.54,
            "discharge_rate_limit_kw": 0.01,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "charge_rate_limit_kw": 17.54,
            "discharge_rate_limit_kw": 0.01,
            "battery_max_charge_power": 17.54,
            "battery_max_discharge_power": 0.01,
        }
        coordinator._pre_control_discharge_limit_kw = 10.6
        entry = types.SimpleNamespace(
            data={"optimization_max_discharge_w": 10600},
            options={},
        )
        coordinator.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda entry_id: entry,
            ),
        )
        coordinator._entry_id = "entry-1"

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [10.6]


def test_sungrow_restore_repairs_blocked_discharge_when_cap_unreadable():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 2.4,
            "battery_level": 98.0,
            "backup_reserve": 20.0,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [10.6]


def test_sungrow_restore_repairs_low_soc_when_floor_and_bms_limit_are_unreadable():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 8.54,
            "load_power": 8.48,
            "battery_level": 4.9,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [10.6]


def test_sungrow_restore_does_not_repair_when_bms_blocks_discharge():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 8.54,
            "load_power": 8.48,
            "battery_level": 4.9,
            "bms_max_discharge_current_a": 0.0,
            "discharge_rate_limit_kw": 0.0,
            "discharge_rate_limit_source": "bms_current",
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_restore_does_not_repair_at_readable_reserve_floor():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 2.4,
            "load_power": 2.35,
            "battery_level": 5.0,
            "backup_reserve": 5.0,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_restore_uses_configured_reserve_when_readback_is_unavailable():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 2.4,
            "load_power": 2.35,
            "battery_level": 10.0,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }
        entry = types.SimpleNamespace(
            data={},
            options={"hardware_backup_reserve": 0.10},
        )
        coordinator.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda entry_id: entry,
            ),
        )
        coordinator._entry_id = "entry-1"

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_restore_repairs_low_load_blocked_discharge_when_cap_unreadable():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 0.232,
            "load_power": 0.227,
            "battery_level": 15.0,
            "backup_reserve": 5.0,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [10.6]


def test_sungrow_restore_does_not_repair_when_low_import_is_not_serving_load():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_power": 0.0,
            "grid_power": 0.16,
            "load_power": 0.5,
            "battery_level": 15.0,
            "backup_reserve": 5.0,
            "charge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
        }

        restore_result = await coordinator.restore_normal()
        return restore_result, fake_controller

    try:
        restore_result, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert restore_result
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_restore_retries_failed_stale_cap_repair():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.fail_discharge_limit_count = 1
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 0.01,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 0.01,
            "battery_max_charge_power": 10.6,
            "battery_max_discharge_power": 0.01,
        }

        first_restore = await coordinator.restore_normal()

        fake_controller.battery_data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 10.6,
        }
        coordinator.data = {
            "charge_rate_limit_kw": 10.6,
            "discharge_rate_limit_kw": 10.6,
            "battery_max_charge_power": 10.6,
            "battery_max_discharge_power": 10.6,
        }
        second_restore = await coordinator.restore_normal()
        return first_restore, second_restore, fake_controller

    try:
        first_restore, second_restore, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert not first_restore
    assert second_restore
    assert fake_controller.restore_normal_calls == 2
    assert fake_controller.discharge_rate_limits == [10.6, 10.6]


def test_sungrow_restore_retries_failed_captured_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restore_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.fail_discharge_limit_count = 1
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}
        coordinator._pre_control_discharge_limit_kw = 15.0

        first_restore = await coordinator.restore_normal()
        second_restore = await coordinator.restore_normal()
        return first_restore, second_restore, fake_controller

    try:
        first_restore, second_restore, fake_controller = asyncio.run(run_restore_cycle())
    finally:
        restore()

    assert not first_restore
    assert second_restore
    assert fake_controller.restore_normal_calls == 2
    assert fake_controller.discharge_rate_limits == [15.0, 15.0]


def test_sungrow_force_discharge_restore_reinstates_previous_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_discharge_cycle():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        force_result = await coordinator.force_discharge(duration_minutes=30, power_w=5000)
        restore_result = await coordinator.restore_normal()
        return force_result, restore_result, fake_controller

    try:
        force_result, restore_result, fake_controller = asyncio.run(run_force_discharge_cycle())
    finally:
        restore()

    assert force_result
    assert restore_result
    assert fake_controller.force_discharge_power_w == [5000]
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_force_discharge_restore_uses_normal_inverter_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_discharge_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.12,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_max_charge_power": 7.9,
            "battery_max_charge_power_w": 7900,
            "battery_max_discharge_power": 4.12,
            "battery_max_discharge_power_w": 4120,
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.12,
        }

        force_result = await coordinator.force_discharge(duration_minutes=30, power_w=500)
        restore_result = await coordinator.restore_normal()
        return force_result, restore_result, fake_controller

    try:
        force_result, restore_result, fake_controller = asyncio.run(run_force_discharge_cycle())
    finally:
        restore()

    assert force_result
    assert restore_result
    assert fake_controller.force_discharge_power_w == [500]
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_spread_export_uses_export_limit_not_discharge_cap():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_export_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_max_charge_power": 7.9,
            "battery_max_charge_power_w": 7900,
            "battery_max_discharge_power": 4.4,
            "battery_max_discharge_power_w": 4400,
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }

        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=4400,
        )
        restore_result = await coordinator.restore_normal()
        return force_result, restore_result, fake_controller

    try:
        force_result, restore_result, fake_controller = asyncio.run(run_force_export_cycle())
    finally:
        restore()

    assert force_result
    assert restore_result
    assert fake_controller.force_discharge_power_w == [7900]
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [7.9, 7.9]
    assert fake_controller.export_limits == [4400, 5000]


def test_sungrow_pending_export_restore_lifecycle_retries_failed_cleanup():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FailingRestoreController(_FakeSungrowController):
        def __init__(self):
            super().__init__()
            self.fail_restore_once = True

        async def set_export_limit(self, watts: int | None) -> bool:
            self.export_limits.append(watts)
            if watts is None and self.fail_restore_once:
                self.fail_restore_once = False
                return False
            return True

    async def run_cycle():
        fake_controller = FailingRestoreController()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 15.0,
            "discharge_rate_limit_kw": 15.0,
        }
        coordinator.data = dict(fake_controller.battery_data)
        fake_controller.force_discharge_result = False

        first_result = await coordinator.force_grid_export(export_limit_w=4400)
        pending_after_failed_cleanup = coordinator.pending_optimizer_export_restore
        second_result = await coordinator.restore_normal()
        return (
            first_result,
            pending_after_failed_cleanup,
            second_result,
            coordinator.pending_optimizer_export_restore,
            fake_controller.export_limits,
        )

    try:
        (
            first_result,
            pending_after_failed_cleanup,
            second_result,
            pending_after_restore,
            export_limits,
        ) = asyncio.run(run_cycle())
    finally:
        restore()

    assert first_result is False
    assert pending_after_failed_cleanup is True
    assert second_result is True
    assert pending_after_restore is False
    assert export_limits == [4400, None, None]


def test_sungrow_partial_force_export_rollback_preserves_restart_ownership():
    """A failed discharge-cap rollback must remain recoverable after restart."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class PartialRollbackController(_FakeSungrowController):
        def __init__(self):
            super().__init__()
            self.fail_discharge_restore = False

        async def set_discharge_rate_limit(self, kw: float) -> bool:
            self.discharge_rate_limits.append(kw)
            if self.fail_discharge_restore:
                return False
            self._rate_limit_writable = True
            return True

        async def force_discharge(self, power_w: float = 5000) -> bool:
            self.force_discharge_power_w.append(power_w)
            self.fail_discharge_restore = True
            return False

    async def run_partial_rollback():
        controller = PartialRollbackController()
        controller.battery_data = {
            "charge_rate_limit_kw": 15.0,
            "discharge_rate_limit_kw": 15.0,
            "export_limit_enabled": True,
            "export_limit_w": 2240,
        }
        store = _FakeStore(
            {
                "active": True,
                "baseline_enabled": True,
                "baseline_limit_w": 2240,
                "target_export_w": 4500,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            controller,
        )
        coordinator._export_control_store = store
        coordinator.data = dict(controller.battery_data)

        force_result = await coordinator.force_grid_export(export_limit_w=4500)
        store_after_partial = dict(store.data)
        pending_after_partial = coordinator.pending_optimizer_export_restore
        controller.fail_discharge_restore = False
        restore_result = await coordinator.restore_normal()
        return (
            force_result,
            store_after_partial,
            pending_after_partial,
            restore_result,
            store,
            coordinator,
            controller,
        )

    try:
        (
            force_result,
            store_after_partial,
            pending_after_partial,
            restore_result,
            store,
            coordinator,
            controller,
        ) = asyncio.run(run_partial_rollback())
    finally:
        restore()

    assert force_result is False
    assert store_after_partial["active"] is True
    assert pending_after_partial is True
    assert restore_result is True
    assert store.data == {"active": False}
    assert coordinator.pending_optimizer_export_restore is False
    assert controller.export_limits == [4500, 2240, 2240]
    assert controller.discharge_rate_limits == [15.0, 15.0, 15.0]


@pytest.mark.parametrize("failure_kind", ("false", "exception"))
def test_sungrow_force_export_failure_restores_ems_before_clearing_ownership(
    failure_kind,
):
    """Force-export rollback must restore EMS and remain retryable on failure."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class EMSFailureController(_FakeSungrowController):
        def __init__(self):
            super().__init__()
            self.restore_results = iter((False, False, True))

        async def force_discharge(self, power_w: float = 5000) -> bool:
            self.force_discharge_power_w.append(power_w)
            self.battery_data.update(
                {
                    "ems_mode": "forced",
                    "ems_mode_name": "Forced",
                }
            )
            if failure_kind == "exception":
                raise RuntimeError("force-discharge failed")
            return False

        async def restore_normal(self) -> bool:
            self.restore_normal_calls += 1
            restored = next(self.restore_results)
            if restored:
                self.battery_data.update(
                    {
                        "ems_mode": "self_consumption",
                        "ems_mode_name": "Self Consumption",
                    }
                )
            return restored

    async def run_failure_and_retries():
        controller = EMSFailureController()
        controller.battery_data = {
            "charge_rate_limit_kw": 15.0,
            "discharge_rate_limit_kw": 15.0,
            "export_limit_enabled": True,
            "export_limit_w": 2240,
            "ems_mode": "self_consumption",
            "ems_mode_name": "Self Consumption",
        }
        store = _FakeStore(
            {
                "active": True,
                "baseline_enabled": True,
                "baseline_limit_w": 2240,
                "target_export_w": 4500,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            controller,
        )
        coordinator._export_control_store = store
        coordinator.data = dict(controller.battery_data)

        raised = None
        force_result = None
        try:
            force_result = await coordinator.force_grid_export(export_limit_w=4500)
        except RuntimeError as err:
            raised = err
        after_force_store = dict(store.data)
        after_force_pending = coordinator.pending_optimizer_export_restore
        after_force_mode = controller.battery_data["ems_mode"]
        after_force_restore_calls = controller.restore_normal_calls

        first_retry = await coordinator.restore_normal()
        after_first_retry_store = dict(store.data)
        after_first_retry_pending = coordinator.pending_optimizer_export_restore
        first_retry_mode = controller.battery_data["ems_mode"]
        second_retry = await coordinator.restore_normal()
        return (
            force_result,
            raised,
            after_force_store,
            after_force_pending,
            after_force_mode,
            after_force_restore_calls,
            first_retry,
            after_first_retry_store,
            after_first_retry_pending,
            first_retry_mode,
            second_retry,
            store,
            coordinator,
            controller,
        )

    try:
        (
            force_result,
            raised,
            after_force_store,
            after_force_pending,
            after_force_mode,
            after_force_restore_calls,
            first_retry,
            after_first_retry_store,
            after_first_retry_pending,
            first_retry_mode,
            second_retry,
            store,
            coordinator,
            controller,
        ) = asyncio.run(run_failure_and_retries())
    finally:
        restore()

    if failure_kind == "exception":
        assert isinstance(raised, RuntimeError)
        assert force_result is None
    else:
        assert raised is None
        assert force_result is False
    assert after_force_store["active"] is True
    assert after_force_pending is True
    assert after_force_mode == "forced"
    assert after_force_restore_calls == 1
    assert first_retry is False
    assert after_first_retry_store["active"] is True
    assert after_first_retry_pending is True
    assert first_retry_mode == "forced"
    assert second_retry is True
    assert store.data == {"active": False}
    assert coordinator.pending_optimizer_export_restore is False
    assert controller.battery_data["ems_mode"] == "self_consumption"
    assert controller.restore_normal_calls == 3


def test_sungrow_partial_normal_restore_remains_pending_until_complete():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class PartialRestoreController(_FakeSungrowController):
        def __init__(self):
            super().__init__()
            self.restore_results = iter((False, True))

        async def restore_normal(self) -> bool:
            self.restore_normal_calls += 1
            return next(self.restore_results)

    async def run_cycle():
        fake_controller = PartialRestoreController()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        coordinator.data = dict(fake_controller.battery_data)

        assert await coordinator.force_grid_export(export_limit_w=4400)
        first_restore = await coordinator.restore_normal()
        pending_after_partial_restore = (
            coordinator.pending_optimizer_export_restore
        )
        second_restore = await coordinator.restore_normal()
        return (
            first_restore,
            pending_after_partial_restore,
            second_restore,
            coordinator.pending_optimizer_export_restore,
            fake_controller,
        )

    try:
        (
            first_restore,
            pending_after_partial_restore,
            second_restore,
            pending_after_complete_restore,
            fake_controller,
        ) = asyncio.run(run_cycle())
    finally:
        restore()

    assert first_restore is False
    assert pending_after_partial_restore is True
    assert second_restore is True
    assert pending_after_complete_restore is False
    assert fake_controller.restore_normal_calls == 2
    assert fake_controller.export_limits == [4400, None, None]


def test_sungrow_user_restore_failure_does_not_claim_optimizer_ownership():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FailedUserRestoreController(_FakeSungrowController):
        async def restore_normal(self) -> bool:
            self.restore_normal_calls += 1
            return False

    async def run_restore():
        fake_controller = FailedUserRestoreController()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        result = await coordinator.restore_normal()
        return result, coordinator.pending_optimizer_export_restore

    try:
        result, pending = asyncio.run(run_restore())
    finally:
        restore()

    assert result is False
    assert pending is False


@pytest.mark.parametrize(
    ("baseline_enabled", "baseline_limit_w", "expected_restore_limit"),
    (
        (False, 0, None),
        (True, 5000, 5000),
    ),
)
def test_sungrow_spread_export_restores_persisted_baseline_after_restart(
    baseline_enabled,
    baseline_limit_w,
    expected_restore_limit,
):
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_restart_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 7.9,
            "export_limit_enabled": baseline_enabled,
            "export_limit_w": baseline_limit_w,
        }
        store = _FakeStore()

        first_coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        first_coordinator._export_control_store = store
        first_coordinator.data = dict(fake_controller.battery_data)
        force_result = await first_coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=2580,
        )
        active_state = dict(store.data)

        restarted_coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        restarted_coordinator._export_control_store = store
        restore_result = (
            await restarted_coordinator.async_restore_persisted_export_control()
        )
        return force_result, restore_result, active_state, store, fake_controller

    try:
        force_result, restore_result, active_state, store, fake_controller = asyncio.run(
            run_restart_cycle()
        )
    finally:
        restore()

    assert force_result
    assert restore_result
    assert active_state == {
        "active": True,
        "baseline_enabled": baseline_enabled,
        "baseline_limit_w": expected_restore_limit,
        "target_export_w": 2580,
    }
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.export_limits == [2580, expected_restore_limit]
    assert store.data == {"active": False}


@pytest.mark.parametrize(
    ("baseline_enabled", "baseline_limit_w", "expected_restore_limit"),
    (
        (False, None, None),
        (True, 5000, 5000),
    ),
)
def test_sungrow_recovery_does_not_recapture_stale_export_target(
    baseline_enabled,
    baseline_limit_w,
    expected_restore_limit,
):
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_recovered_export_cycle():
        old_target_w = 2580
        new_target_w = 4400
        fake_controller = _FakeSungrowController()
        store = _FakeStore(
            {
                "active": True,
                "baseline_enabled": baseline_enabled,
                "baseline_limit_w": baseline_limit_w,
                "target_export_w": old_target_w,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        coordinator._export_control_store = store
        coordinator.data = {
            "battery_max_charge_power": 7.9,
            "battery_max_charge_power_w": 7900,
            "battery_max_discharge_power": 7.9,
            "battery_max_discharge_power_w": 7900,
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 7.9,
            # A coordinator sample taken while the interrupted temporary
            # export target was still active.
            "export_limit_enabled": True,
            "export_limit_w": old_target_w,
        }

        recovery_result = await coordinator.async_restore_persisted_export_control()
        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=new_target_w,
        )
        active_state = dict(store.data)
        restore_result = await coordinator.restore_normal()
        return (
            recovery_result,
            force_result,
            restore_result,
            active_state,
            store,
            fake_controller,
            coordinator,
        )

    try:
        (
            recovery_result,
            force_result,
            restore_result,
            active_state,
            store,
            fake_controller,
            coordinator,
        ) = asyncio.run(run_recovered_export_cycle())
    finally:
        restore()

    assert recovery_result
    assert force_result
    assert restore_result
    assert active_state == {
        "active": True,
        "baseline_enabled": baseline_enabled,
        "baseline_limit_w": expected_restore_limit,
        "target_export_w": 4400,
    }
    assert fake_controller.export_limits == [
        expected_restore_limit,
        4400,
        expected_restore_limit,
    ]
    assert store.data == {"active": False}
    assert not coordinator._pre_control_export_limit_captured


@pytest.mark.parametrize(
    ("baseline_enabled", "baseline_limit_w", "expected_restore_limit"),
    (
        (False, None, None),
        (True, 2240, 2240),
    ),
)
def test_sungrow_first_refresh_publishes_recovered_export_baseline(
    baseline_enabled,
    baseline_limit_w,
    expected_restore_limit,
):
    """A restart snapshot must not republish the interrupted export target."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_recovered_refresh_cycle():
        old_target_w = 4500
        new_target_w = 4251
        class RecoveredController(_FakeSungrowController):
            async def restore_normal(self):
                self.restore_normal_calls += 1
                self.battery_data.update(
                    {
                        "export_limit_enabled": baseline_enabled,
                        "export_limit_w": baseline_limit_w or 0,
                        "ems_mode": "self_consumption",
                        "ems_mode_name": "Self Consumption",
                    }
                )
                return True

        fake_controller = RecoveredController()
        fake_controller.battery_data.update(
            {
                "battery_soc": 54.0,
                "battery_power": 5600,
                "meter_power": -4500,
                "load_power": 900,
                "pv_power": 0,
                "daily_pv_generation": 0,
                "export_limit_enabled": True,
                "export_limit_w": old_target_w,
                "ems_mode": "forced",
                "ems_mode_name": "Forced",
            }
        )
        store = _FakeStore(
            {
                "active": True,
                "baseline_enabled": baseline_enabled,
                "baseline_limit_w": baseline_limit_w,
                "target_export_w": old_target_w,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        coordinator.hass = types.SimpleNamespace(data={"power_sync": {"entry-1": {}}})
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._export_control_store = store
        coordinator._persisted_export_control_recovery_pending = True

        first_snapshot = await coordinator._async_update_data()
        # Match DataUpdateCoordinator's assignment after a successful refresh.
        coordinator.data = first_snapshot
        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=new_target_w,
        )
        active_state = dict(store.data)
        restore_result = await coordinator.restore_normal()
        return (
            first_snapshot,
            force_result,
            restore_result,
            active_state,
            store,
            fake_controller,
        )

    try:
        (
            first_snapshot,
            force_result,
            restore_result,
            active_state,
            store,
            fake_controller,
        ) = asyncio.run(run_recovered_refresh_cycle())
    finally:
        restore()

    assert first_snapshot["export_limit_enabled"] is baseline_enabled
    assert first_snapshot["export_limit_w"] == expected_restore_limit
    assert first_snapshot["ems_mode"] == "self_consumption"
    assert first_snapshot["ems_mode_name"] == "Self Consumption"
    assert force_result is True
    assert restore_result is True
    assert active_state == {
        "active": True,
        "baseline_enabled": baseline_enabled,
        "baseline_limit_w": expected_restore_limit,
        "target_export_w": 4251,
    }
    assert fake_controller.export_limits == [
        expected_restore_limit,
        4251,
        expected_restore_limit,
    ]
    assert store.data == {"active": False}


def test_sungrow_failed_recovery_keeps_persisted_ownership_for_retry():
    """A partial restore must not consume persisted export ownership."""
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    class FlakyRecoveryController(_FakeSungrowController):
        def __init__(self):
            super().__init__()
            self.battery_data = {
                "battery_soc": 54.0,
                "battery_power": 0,
                "meter_power": 0,
                "load_power": 0,
                "pv_power": 0,
                "daily_pv_generation": 0,
                "charge_rate_limit_kw": 15.0,
                "discharge_rate_limit_kw": 15.0,
                "export_limit_enabled": True,
                "export_limit_w": 4500,
                "ems_mode": "forced",
                "ems_mode_name": "Forced",
            }
            self.restore_results = iter((False, True))

        async def restore_normal(self):
            self.restore_normal_calls += 1
            restored = next(self.restore_results)
            if restored:
                self.battery_data.update(
                    {
                        "ems_mode": "self_consumption",
                        "ems_mode_name": "Self Consumption",
                    }
                )
            return restored

        async def set_export_limit(self, watts):
            self.export_limits.append(watts)
            self.battery_data["export_limit_enabled"] = watts is not None
            self.battery_data["export_limit_w"] = watts or 0
            return True

    async def run_recovery_retries():
        controller = FlakyRecoveryController()
        store = _FakeStore(
            {
                "active": True,
                "baseline_enabled": True,
                "baseline_limit_w": 2240,
                "target_export_w": 4500,
            }
        )
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            controller,
        )
        coordinator.hass = types.SimpleNamespace(
            data={"power_sync": {"entry-1": {}}}
        )
        coordinator._entry_id = "entry-1"
        coordinator._energy_acc = _FakeEnergyAccumulator()
        coordinator._export_control_store = store
        coordinator._persisted_export_control_recovery_pending = True

        first = await coordinator._async_update_data()
        first_store = dict(store.data)
        coordinator.data = first
        second = await coordinator._async_update_data()
        return first, first_store, second, store, controller

    try:
        first, first_store, second, store, controller = asyncio.run(
            run_recovery_retries()
        )
    finally:
        restore()

    assert first["telemetry_ready"] is False
    assert first_store["active"] is True
    assert second["telemetry_ready"] is True
    assert second["export_limit_w"] == 2240
    assert second["ems_mode"] == "self_consumption"
    assert second["ems_mode_name"] == "Self Consumption"
    assert store.data == {"active": False}
    assert controller.restore_normal_calls == 2
    assert controller.export_limits == [2240, 2240]


def test_sungrow_spread_export_refuses_hardware_write_when_state_cannot_persist():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_failed_persistence():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(
            SungrowEnergyCoordinator,
            fake_controller,
        )
        coordinator._export_control_store = _FakeStore(fail_save=True)
        coordinator.data = dict(fake_controller.battery_data)
        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=2580,
        )
        return force_result, fake_controller

    try:
        force_result, fake_controller = asyncio.run(run_failed_persistence())
    finally:
        restore()

    assert not force_result
    assert fake_controller.discharge_rate_limits == []
    assert fake_controller.export_limits == []
    assert fake_controller.force_discharge_power_w == []


def test_sungrow_spread_export_continues_when_discharge_limit_unsupported():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_export_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.fail_discharge_limit = True
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_max_charge_power": 7.9,
            "battery_max_charge_power_w": 7900,
            "battery_max_discharge_power": 4.4,
            "battery_max_discharge_power_w": 4400,
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }

        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=4400,
        )
        restore_result = await coordinator.restore_normal()
        return force_result, restore_result, fake_controller

    try:
        force_result, restore_result, fake_controller = asyncio.run(run_force_export_cycle())
    finally:
        restore()

    assert force_result
    assert restore_result
    assert fake_controller.force_discharge_power_w == [7900]
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.discharge_rate_limits == [7.9]
    assert fake_controller.export_limits == [4400, 5000]


def test_sungrow_spread_export_failure_restores_export_limit_and_discharge_cap():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_export_failure():
        fake_controller = _FakeSungrowController()
        fake_controller.force_discharge_result = False
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 7.9,
            "discharge_rate_limit_kw": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "battery_max_charge_power": 7.9,
            "battery_max_discharge_power": 4.4,
            "export_limit_enabled": True,
            "export_limit_w": 5000,
        }

        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=4400,
        )
        return force_result, fake_controller

    try:
        force_result, fake_controller = asyncio.run(run_force_export_failure())
    finally:
        restore()

    assert not force_result
    assert fake_controller.force_discharge_power_w == [7900]
    assert fake_controller.discharge_rate_limits == [7.9, 7.9]
    assert fake_controller.export_limits == [4400, 5000]


def test_sungrow_spread_export_clamps_headroom_to_configured_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_export_cycle():
        fake_controller = _FakeSungrowController()
        fake_controller.battery_data = {
            "charge_rate_limit_kw": 23.8,
            "discharge_rate_limit_kw": 0.01,
            "export_limit_enabled": False,
            "export_limit_w": 0,
        }
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {
            "charge_rate_limit_kw": 23.8,
            "battery_max_charge_power": 23.8,
            "discharge_rate_limit_kw": 0.01,
            "battery_max_discharge_power": 0.01,
        }
        entry = types.SimpleNamespace(
            data={"optimization_max_discharge_w": 20000},
            options={},
        )
        coordinator.hass = types.SimpleNamespace(
            config_entries=types.SimpleNamespace(
                async_get_entry=lambda entry_id: entry,
            ),
        )
        coordinator._entry_id = "entry-1"

        force_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=1150,
        )
        return force_result, fake_controller

    try:
        force_result, fake_controller = asyncio.run(run_force_export_cycle())
    finally:
        restore()

    assert force_result
    assert fake_controller.discharge_rate_limits == [20.0]
    assert fake_controller.force_discharge_power_w == [20000]
    assert fake_controller.export_limits == [1150]


def test_sungrow_spread_export_skips_known_unwritable_discharge_limit_register():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_export_cycles():
        fake_controller = _FakeSungrowController()
        fake_controller.fail_discharge_limit_count = 1
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        first_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=4400,
        )
        second_result = await coordinator.force_grid_export(
            duration_minutes=30,
            export_limit_w=4500,
        )
        return first_result, second_result, fake_controller

    try:
        first_result, second_result, fake_controller = asyncio.run(run_force_export_cycles())
    finally:
        restore()

    assert first_result
    assert second_result
    assert fake_controller.discharge_rate_limits == [15.0]
    assert fake_controller.force_discharge_power_w == [15000, 15000]
    assert fake_controller.export_limits == [4400, 4500]


def test_sungrow_force_discharge_failure_restores_previous_discharge_limit():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_discharge_failure():
        fake_controller = _FakeSungrowController()
        fake_controller.force_discharge_result = False
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_discharge_power": 15.0}

        force_result = await coordinator.force_discharge(duration_minutes=30, power_w=500)
        return force_result, fake_controller

    try:
        force_result, fake_controller = asyncio.run(run_force_discharge_failure())
    finally:
        restore()

    assert not force_result
    assert fake_controller.force_discharge_power_w == [500]
    assert fake_controller.discharge_rate_limits == []


def test_sungrow_force_charge_leaves_existing_charge_limit_unchanged():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_charge_cycle():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.data = {"battery_max_charge_power": 15.0}

        force_result = await coordinator.force_charge(duration_minutes=30, power_w=5300)
        restore_result = await coordinator.restore_normal()
        return force_result, restore_result, fake_controller

    try:
        force_result, restore_result, fake_controller = asyncio.run(run_force_charge_cycle())
    finally:
        restore()

    assert force_result
    assert restore_result
    assert fake_controller.force_charge_power_w == [5300]
    assert fake_controller.restore_normal_calls == 1
    assert fake_controller.charge_rate_limits == []


def test_sungrow_coordinator_serializes_force_charge_with_modbus_lock():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_charge_while_locked():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)

        async with coordinator._modbus_lock:
            task = asyncio.create_task(
                coordinator.force_charge(duration_minutes=30, power_w=12000)
            )
            await asyncio.sleep(0)
            assert fake_controller.force_charge_power_w == []

        result = await task
        return result, fake_controller

    try:
        result, fake_controller = asyncio.run(run_force_charge_while_locked())
    finally:
        restore()

    assert result
    assert fake_controller.charge_rate_limits == []
    assert fake_controller.force_charge_power_w == [12000]


def test_sungrow_coordinator_shutdown_waits_for_modbus_lock():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_shutdown_while_locked():
        fake_controller = _FakeSungrowController()
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, fake_controller)
        coordinator.update_interval = object()

        async with coordinator._modbus_lock:
            task = asyncio.create_task(coordinator.async_shutdown())
            await asyncio.sleep(0)
            assert fake_controller.disconnect_calls == 0
            assert coordinator.update_interval is None

        await task
        return fake_controller

    try:
        fake_controller = asyncio.run(run_shutdown_while_locked())
    finally:
        restore()

    assert fake_controller.disconnect_calls == 1
