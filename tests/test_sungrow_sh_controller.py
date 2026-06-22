"""Regression tests for Sungrow SH Modbus force-mode writes."""

from __future__ import annotations

import asyncio
from datetime import datetime
import importlib
import importlib.util
from pathlib import Path
import sys
import types


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
    assert data["export_limit_enabled"] is True
    assert data["backup_reserve"] == 30
    assert (controller.REG_MAX_CHARGE_POWER, 1) in holding_reads
    assert (controller.REG_MAX_DISCHARGE_POWER, 1) in holding_reads
    assert (controller.REG_BMS_MAX_CHARGE_CURRENT, 1) not in input_reads
    assert (13065, 1) not in holding_reads
    assert (13066, 1) not in holding_reads


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
        self.force_discharge_result = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def set_charge_rate_limit(self, kw: float) -> bool:
        self.charge_rate_limits.append(kw)
        return True

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        self.discharge_rate_limits.append(kw)
        if kw == 0 and self.fail_zero_discharge_limit:
            return False
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
    coordinator._modbus_lock = asyncio.Lock()
    coordinator._total_import_baseline = None
    coordinator._total_export_baseline = None
    coordinator._baseline_date = None
    return coordinator


def test_sungrow_coordinator_includes_ac_inverter_power_in_home_load():
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
            }

    async def run_update():
        coordinator = _new_sungrow_coordinator(SungrowEnergyCoordinator, FakeController())
        coordinator.hass = types.SimpleNamespace(
            data={
                "power_sync": {
                    "entry-1": {
                        "inverter_attributes": {
                            "power_output_w": 3119,
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

    assert data["solar_power"] == 4.55
    assert data["ac_inverter_solar_power"] == 3.119
    assert data["battery_temp"] == 20.8
    assert data["inverter_temperature"] == 31.2
    assert round(data["load_power"], 3) == 1.979
    assert round(energy_acc.updates[-1][3], 3) == 1.979


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
