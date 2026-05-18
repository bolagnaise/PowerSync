"""Regression tests for Sungrow SH Modbus force-mode writes."""

from __future__ import annotations

import asyncio
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
    const.CONF_FLEET_API_BASE_URL = "fleet_api_base_url"
    const.TESLA_SITE_INFO_CACHE_TTL_SECONDS = 3600
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
    read_values = [
        [controller.EMS_SELF_CONSUMPTION],
        [controller.EMS_FORCED],
    ]

    async def connect() -> bool:
        return True

    async def write_register(address: int, value: int) -> bool:
        writes.append((address, value))
        return True

    async def read_register(address: int, count: int = 1):
        return read_values.pop(0)

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


class _FakeSungrowController:
    def __init__(self):
        self.charge_rate_limits: list[float] = []
        self.discharge_rate_limits: list[float] = []
        self.force_charge_power_w: list[float] = []
        self.force_discharge_power_w: list[float] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def set_charge_rate_limit(self, kw: float) -> bool:
        self.charge_rate_limits.append(kw)
        return True

    async def set_discharge_rate_limit(self, kw: float) -> bool:
        self.discharge_rate_limits.append(kw)
        return True

    async def force_charge(self, power_w: float = 5000) -> bool:
        self.force_charge_power_w.append(power_w)
        return True

    async def force_discharge(self, power_w: float = 5000) -> bool:
        self.force_discharge_power_w.append(power_w)
        return True


def test_sungrow_coordinator_passes_requested_discharge_power_to_controller():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_discharge():
        fake_controller = _FakeSungrowController()
        coordinator = SungrowEnergyCoordinator.__new__(SungrowEnergyCoordinator)
        coordinator._controller = fake_controller

        result = await coordinator.force_discharge(duration_minutes=30, power_w=20000)
        return result, fake_controller

    try:
        result, fake_controller = asyncio.run(run_force_discharge())
    finally:
        restore()

    assert result
    assert fake_controller.discharge_rate_limits == [20]
    assert fake_controller.force_discharge_power_w == [20000]


def test_sungrow_coordinator_passes_requested_charge_power_to_controller():
    SungrowEnergyCoordinator, restore = _load_sungrow_energy_coordinator()

    async def run_force_charge():
        fake_controller = _FakeSungrowController()
        coordinator = SungrowEnergyCoordinator.__new__(SungrowEnergyCoordinator)
        coordinator._controller = fake_controller

        result = await coordinator.force_charge(duration_minutes=30, power_w=12000)
        return result, fake_controller

    try:
        result, fake_controller = asyncio.run(run_force_charge())
    finally:
        restore()

    assert result
    assert fake_controller.charge_rate_limits == [12]
    assert fake_controller.force_charge_power_w == [12000]
