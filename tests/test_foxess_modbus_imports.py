"""Regression tests for FoxESS direct Modbus pymodbus imports."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"


def _snapshot_modules() -> dict[str, types.ModuleType]:
    return {
        name: module
        for name, module in sys.modules.items()
        if name == "pymodbus"
        or name.startswith("pymodbus.")
        or name == "power_sync"
        or name.startswith("power_sync.")
    }


def _restore_modules(snapshot: dict[str, types.ModuleType]) -> None:
    for name in list(sys.modules):
        if (
            name == "pymodbus"
            or name.startswith("pymodbus.")
            or name == "power_sync"
            or name.startswith("power_sync.")
        ):
                sys.modules.pop(name, None)
    sys.modules.update(snapshot)


def _clear_test_modules() -> None:
    _restore_modules({})


def _install_power_sync_package() -> None:
    power_sync = types.ModuleType("power_sync")
    power_sync.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = power_sync

    inverters = types.ModuleType("power_sync.inverters")
    inverters.__path__ = [str(COMPONENT_ROOT / "inverters")]
    sys.modules["power_sync.inverters"] = inverters


def _write_fake_pymodbus(root: Path) -> None:
    package = root / "pymodbus"
    client = package / "client"
    framer = package / "framer"
    client.mkdir(parents=True)
    framer.mkdir(parents=True)

    (package / "__init__.py").write_text('__version__ = "3.10.0"\n', encoding="utf-8")
    (client / "__init__.py").write_text(
        "from pymodbus.client.tcp import AsyncModbusTcpClient\n"
        "from pymodbus.client.serial import AsyncModbusSerialClient\n",
        encoding="utf-8",
    )
    (framer / "__init__.py").write_text(
        "class FramerType:\n"
        "    SOCKET = 'socket'\n",
        encoding="utf-8",
    )
    (package / "exceptions.py").write_text(
        "class ModbusException(Exception):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (client / "tcp.py").write_text(
        "from pymodbus.framer import FramerType\n"
        "\n"
        "class AsyncModbusTcpClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.kwargs = kwargs\n"
        "\n"
        "    async def connect(self):\n"
        "        return True\n"
        "\n"
        "    def close(self):\n"
        "        pass\n"
        "\n"
        "    async def read_input_registers(self, address, count=1, slave=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    (client / "serial.py").write_text(
        "from pymodbus.framer import FramerType\n"
        "\n"
        "class AsyncModbusSerialClient:\n"
        "    def __init__(self, **kwargs):\n"
        "        self.kwargs = kwargs\n"
        "\n"
        "    async def connect(self):\n"
        "        return True\n"
        "\n"
        "    def close(self):\n"
        "        pass\n",
        encoding="utf-8",
    )


def _vendored_framer_module() -> types.ModuleType:
    module = types.ModuleType("pymodbus.framer")
    module.__file__ = (
        "/config/custom_components/foxess_modbus/vendor/pymodbus/"
        "pymodbus-3.6.9/pymodbus/framer/__init__.py"
    )
    return module


def test_foxess_direct_tcp_import_discards_vendored_foxess_modbus_framer(
    tmp_path: Path,
):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        sys.modules["pymodbus.framer"] = _vendored_framer_module()

        module = importlib.import_module("power_sync.inverters.foxess")

        assert module.AsyncModbusTcpClient.__module__ == "pymodbus.client.tcp"
        framer_file = str(sys.modules["pymodbus.framer"].__file__).replace("\\", "/")
        assert "/foxess_modbus/vendor/pymodbus/" not in framer_file
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_foxess_serial_client_import_discards_late_vendored_framer(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")

        sys.modules["pymodbus.framer"] = _vendored_framer_module()
        sys.modules.pop("pymodbus.client.serial", None)
        controller = module.FoxESSController(
            host="192.0.2.1",
            connection_type="serial",
            serial_port="/dev/ttyUSB0",
        )

        assert asyncio.run(controller.connect())
        assert controller._client.__class__.__module__ == "pymodbus.client.serial"
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_h3_smart_direct_modbus_reads_pv3_power(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")
        controller = module.FoxESSController(
            host="192.0.2.1",
            model_family="H3-Smart",
        )

        reads: list[tuple[int, int]] = []
        holding_registers = {
            37612: [62],
            39237: [0, 0],
            39279: [0, 1000],
            39281: [0, 2000],
            39283: [0, 1500],
            38814: [0, 0],
            38914: [0, 0],
            49203: [1],
            46611: [10],
            46607: [250],
            46608: [250],
            39227: [5000],
            37611: [240],
            37624: [100],
            39053: [0, 15000],
            37635: [1000],
            39625: [0, 0],
        }

        async def fake_read_holding(address: int, count: int = 1):
            reads.append((address, count))
            return holding_registers.get(address, [0] * count)

        controller._read_holding_registers = fake_read_holding

        status = asyncio.run(controller.get_status())

        assert status.status == module.InverterStatus.ONLINE
        assert status.attributes["pv1_power_kw"] == 1.0
        assert status.attributes["pv2_power_kw"] == 2.0
        assert status.attributes["pv3_power_kw"] == 1.5
        assert status.attributes["pv_power_kw"] == 4.5
        assert status.power_output_w == 4500.0
        assert (39283, 2) in reads
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_h1_kh_h3_direct_modbus_uses_pv2_power_register(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")

        for family in (
            module.FoxESSModelFamily.H1,
            module.FoxESSModelFamily.KH,
            module.FoxESSModelFamily.H3,
        ):
            register_map = module.REGISTER_MAPS[family]

            assert register_map.pv1_power == 31002
            assert register_map.pv2_power == 31005
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_kh_direct_modbus_normalizes_grid_import_sign(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")
        controller = module.FoxESSController(
            host="192.0.2.1",
            model_family="KH",
        )

        def signed16_word(value: int) -> int:
            return value & 0xFFFF

        data_registers = {
            31024: [34],
            31022: [signed16_word(-5780)],
            31002: [780],
            31005: [0],
            # KH reports forced grid charge/import as a negative raw grid value.
            31008: [signed16_word(-5000)],
            31016: [220],
            31020: [512],
            31021: [0],
            31023: [250],
        }
        holding_registers = {
            41000: [2],
            41009: [10],
            41007: [250],
            41008: [250],
        }

        async def fake_read_data(address: int, count: int = 1):
            return data_registers.get(address, [0] * count)

        async def fake_read_holding(address: int, count: int = 1):
            return holding_registers.get(address, [0] * count)

        controller._read_data_register = fake_read_data
        controller._read_holding_registers = fake_read_holding

        status = asyncio.run(controller.get_status())

        assert status.status == module.InverterStatus.ONLINE
        assert status.attributes["model_family"] == "KH"
        assert status.attributes["work_mode_name"] == "Backup"
        assert status.attributes["battery_power_kw"] == -5.78
        assert status.attributes["grid_power_kw"] == 5.0
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_kh_daily_energy_retries_holding_registers_when_input_read_fails(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")
        controller = module.FoxESSController(
            host="192.0.2.1",
            model_family="KH",
        )

        input_reads: list[tuple[int, int]] = []
        holding_reads: list[tuple[int, int]] = []

        async def fake_read_data(address: int, count: int = 1):
            input_reads.append((address, count))
            return None

        async def fake_read_holding(address: int, count: int = 1):
            holding_reads.append((address, count))
            return {
                31088: [164],
                31089: [66],
            }.get(address)

        controller._read_data_register = fake_read_data
        controller._read_holding_registers = fake_read_holding

        summary = asyncio.run(controller.get_energy_summary())

        assert summary == {
            "charge_today_kwh": 16.4,
            "discharge_today_kwh": 6.6,
        }
        assert input_reads == [(31088, 1), (31089, 1)]
        assert holding_reads == [(31088, 1), (31089, 1)]
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)


def test_h3_smart_direct_modbus_keeps_last_valid_calculated_load(tmp_path: Path):
    snapshot = _snapshot_modules()
    original_path = list(sys.path)
    try:
        _clear_test_modules()
        _write_fake_pymodbus(tmp_path)
        sys.path.insert(0, str(tmp_path))
        _install_power_sync_package()
        module = importlib.import_module("power_sync.inverters.foxess")
        controller = module.FoxESSController(
            host="192.0.2.1",
            model_family="H3-Smart",
        )

        def signed32_words(value: int) -> list[int]:
            value &= 0xFFFFFFFF
            return [(value >> 16) & 0xFFFF, value & 0xFFFF]

        readings = [
            {
                "battery_power_w": -2000,
                "grid_power_w": 1000,
                "pv_power_w": 6000,
            },
            {
                "battery_power_w": -20000,
                "grid_power_w": -1000,
                "pv_power_w": 6000,
            },
        ]
        poll_index = 0

        async def fake_read_holding(address: int, count: int = 1):
            current = readings[poll_index]
            holding_registers = {
                37612: [60],
                39237: signed32_words(current["battery_power_w"]),
                39279: signed32_words(current["pv_power_w"]),
                39281: [0, 0],
                39283: [0, 0],
                # H3-Smart raw grid sign is inverted: positive raw means export.
                38814: signed32_words(-current["grid_power_w"] * 10),
                38914: [0, 0],
                49203: [1],
                46611: [10],
                46607: [250],
                46608: [250],
                39227: [5000],
                37611: [240],
                37624: [100],
                39053: [0, 15000],
                37635: [1000],
                39625: [0, 0],
            }
            return holding_registers.get(address, [0] * count)

        controller._read_holding_registers = fake_read_holding

        first = asyncio.run(controller.get_status())
        poll_index = 1
        second = asyncio.run(controller.get_status())

        assert first.status == module.InverterStatus.ONLINE
        assert first.attributes["load_power_kw"] == 5.0
        assert second.status == module.InverterStatus.ONLINE
        assert second.attributes["load_power_kw"] == 5.0
    finally:
        sys.path[:] = original_path
        _restore_modules(snapshot)
