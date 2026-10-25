"""Native iHM dispatch protocol and safety tests; no hardware connections."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace, ModuleType

import pytest


@pytest.fixture
def driver(monkeypatch):
    package = ModuleType("pymodbus")
    client_module = ModuleType("pymodbus.client")
    class ImportClient:
        async def read_input_registers(self, *, address, count, device_id=1):
            raise AssertionError("Unmocked transport")
    client_module.AsyncModbusTcpClient = ImportClient
    monkeypatch.setitem(sys.modules, "pymodbus", package)
    monkeypatch.setitem(sys.modules, "pymodbus.client", client_module)
    path = Path(__file__).resolve().parents[1] / "custom_components/power_sync/inverters/sungrow_ihomemanager.py"
    name = "ihomemanager_test_driver"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class FakeDevice:
    def __init__(self):
        # V1.0.2 example on-wire words: 02 00 01 00, low word first.
        self.identity = [0x072A, 0x5741, 0x0030, 0x0200, 0x0100]
        self.data = [200, 0, 200, 0, 100, 0, 0xFF9C, 0xFFFF, 50, 0, 25, 0, 650]
        self.mode = [1, 0xCC, 0, 0]
        self.heartbeat = [600]
        self.calls = []
        self.connections = 0
        self.fail_write = False
        self.apply_failed_write = False
        self.fail_reads = False
        self.wrong_mode_readback = False
        self.heartbeat_readback = True
        self.connected = True
        self.open_count = 0
        self.max_open_count = 0

    def factory(self, *args, **kwargs):
        device = self

        class Client:
            async def connect(self):
                device.connections += 1
                device.open_count += 1
                device.max_open_count = max(device.open_count, device.max_open_count)
                return device.connected

            def close(self):
                device.open_count -= 1

            async def read_input_registers(self, *, address, count, **kwargs):
                assert kwargs == {"device_id": 247} or kwargs == {"slave": 247}
                device.calls.append(("input", address, count))
                words = device.identity if address == 7999 else device.data
                return SimpleNamespace(isError=lambda: device.fail_reads, registers=words[:count])

            async def read_holding_registers(self, *, address, count, **kwargs):
                assert kwargs == {"device_id": 247} or kwargs == {"slave": 247}
                device.calls.append(("holding", address, count))
                words = device.mode[:] if address == 8023 else device.heartbeat[:]
                if address == 8023 and device.wrong_mode_readback and device.mode[0] == 4:
                    words[2] += 1
                if address == 8032 and not device.heartbeat_readback:
                    words = [600]
                return SimpleNamespace(isError=lambda: device.fail_reads, registers=words[:count])

            async def write_registers(self, *, address, values, **kwargs):
                assert kwargs == {"device_id": 247} or kwargs == {"slave": 247}
                device.calls.append(("write", address, list(values)))
                if not device.fail_write or device.apply_failed_write:
                    if address == 8023:
                        device.mode = list(values)
                    else:
                        device.heartbeat = list(values)
                return SimpleNamespace(isError=lambda: device.fail_write)

        return Client()


@pytest.fixture
def device(driver, monkeypatch):
    device = FakeDevice()
    monkeypatch.setattr(driver, "AsyncModbusTcpClient", device.factory)
    return device


def writes(device):
    return [call for call in device.calls if call[0] == "write"]


def test_read_only_snapshot_preserves_native_units_and_directions(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test")
        snapshot = await controller.read_snapshot()
        assert snapshot.supports_dispatch
        assert snapshot.max_discharge_w == 20000
        assert snapshot.soc_percent == 65
        assert snapshot.raw_grid_power_w == -1000
        assert snapshot.raw_battery_power_w == 250
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert not writes(device)
        assert device.open_count == 0
    asyncio.run(run())


@pytest.mark.parametrize("action,command", [("charge", 0xAA), ("discharge", 0xBB)])
def test_native_atomic_dispatch_and_restore(driver, device, action, command):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch(action, 5050, 60)
        assert writes(device) == [("write", 8032, [20]), ("write", 8023, [4, command, 50, 0])]
        assert await controller.stop_dispatch()
        assert device.mode == [1, 0xCC, 0, 0]
        assert not controller._owns_dispatch
        assert device.open_count == 0
        assert all(call[1] < 9000 for call in device.calls)
    asyncio.run(run())


def test_target_is_clamped_to_live_battery_limit(driver, device):
    async def run():
        device.data[2] = 71
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 20000, 60)
        assert device.mode == [4, 0xBB, 71, 0]
        assert await controller.async_shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", [
    "wrong_device", "old_protocol", "unknown_protocol", "invalid_soc", "sentinel_soc",
    "zero_limit", "sentinel_limit", "signed_sentinel", "external_vpp", "compulsory",
    "below_reserve", "full_battery", "invalid_identity", "read_error",
])
def test_invalid_or_external_state_never_writes(driver, device, mutation):
    async def run():
        action = "discharge"
        if mutation == "wrong_device": device.identity[0] = 0x072B
        elif mutation == "old_protocol": device.identity[3] = 0x0100
        elif mutation == "unknown_protocol": device.identity[3] = 0x0300
        elif mutation == "invalid_soc": device.data[12] = 1001
        elif mutation == "sentinel_soc": device.data[12] = 0xFFFF
        elif mutation == "zero_limit": device.data[2] = 0
        elif mutation == "sentinel_limit": device.data[2] = 0xFFFF
        elif mutation == "signed_sentinel": device.data[6:8] = [0xFFFF, 0x7FFF]
        elif mutation == "external_vpp": device.mode[0] = 4
        elif mutation == "compulsory": device.mode[0] = 5
        elif mutation == "below_reserve": device.data[12] = 200
        elif mutation == "full_battery":
            device.data[12] = 1000
            action = "charge"
        elif mutation == "invalid_identity": device.identity[1:3] = [0xFFFF, 0xFFFF]
        elif mutation == "read_error": device.fail_reads = True
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert not await controller.start_dispatch(action, 5000, 60)
        assert not writes(device)
        assert device.open_count == 0
    asyncio.run(run())


@pytest.mark.parametrize("power,duration", [(float("nan"), 60), (float("inf"), 60), (0, 60), (99, 60), (True, 60), (1000, 0), (1000, 86401), (1000, float("nan")), (1000, True)])
def test_invalid_request_never_connects(driver, device, power, duration):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        with pytest.raises(ValueError):
            await controller.start_dispatch("discharge", power, duration)
        assert device.connections == 0
    asyncio.run(run())


def test_heartbeat_readback_required_before_entering_vpp(driver, device):
    async def run():
        device.heartbeat_readback = False
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert device.mode[0] == 1
        assert writes(device) == [("write", 8032, [20])]
    asyncio.run(run())


def test_mode_readback_mismatch_does_not_report_success(driver, device):
    async def run():
        device.wrong_mode_readback = True
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert controller._task is None
        # Mismatching state may belong to an external controller: never overwrite it.
        assert len(writes(device)) == 2
    asyncio.run(run())


def test_expiry_restores_without_renewing(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 0.01)
        await asyncio.wait_for(controller._task, timeout=1)
        assert device.mode == [1, 0xCC, 0, 0]
        assert len([call for call in writes(device) if call[1:] == (8032, [20])]) == 1
    asyncio.run(run())


@pytest.mark.parametrize("reason", ["permission", "soc", "limit", "ownership", "connection"])
def test_renewal_fails_closed(driver, device, reason):
    async def run():
        allowed = True
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: allowed)
        controller.RENEW_SECONDS = 0.001
        assert await controller.start_dispatch("discharge", 5000, 60)
        if reason == "permission": allowed = False
        elif reason == "soc": device.data[12] = 200
        elif reason == "limit": device.data[2] = 10
        elif reason == "ownership": device.mode = [5, 0xAA, 75, 0]
        elif reason == "connection": device.connected = False
        await asyncio.wait_for(controller._task, timeout=1)
        assert len([call for call in writes(device) if call[1:] == (8032, [20])]) == 1
        if reason == "ownership":
            assert device.mode == [5, 0xAA, 75, 0]
        elif reason == "connection":
            assert controller._owns_dispatch  # Cleanup state retained for retry.
        else:
            assert device.mode == [1, 0xCC, 0, 0]
    asyncio.run(run())


def test_failed_restore_is_retryable_and_blocks_replacement(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        device.fail_write = True
        assert not await controller.stop_dispatch()
        assert controller._owns_dispatch
        assert not await controller.start_dispatch("charge", 5000, 60)
        device.fail_write = False
        assert await controller.stop_dispatch()
        assert device.mode == [1, 0xCC, 0, 0]
    asyncio.run(run())


def test_shutdown_restores_and_permanently_blocks_new_dispatch(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        assert await controller.async_shutdown()
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert device.mode == [1, 0xCC, 0, 0]
    asyncio.run(run())


def test_concurrent_replacements_leave_only_one_heartbeat_task(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        results = await asyncio.gather(
            controller.start_dispatch("discharge", 5000, 60),
            controller.start_dispatch("charge", 1000, 60),
        )
        assert results == [True, True]
        tasks = [task for task in asyncio.all_tasks() if task.get_name() == "power_sync_ihomemanager_dispatch"]
        assert len(tasks) == 1
        assert device.mode == [4, 0xAA, 10, 0]
        assert device.max_open_count == 1
        assert await controller.async_shutdown()
    asyncio.run(run())


def test_restores_original_heartbeat_after_dispatch(driver, device):
    async def run():
        device.heartbeat = [120]
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        assert device.heartbeat == [20]
        assert await controller.stop_dispatch()
        assert device.heartbeat == [120]
        assert writes(device)[-2:] == [("write", 8023, [1, 0xCC, 0, 0]), ("write", 8032, [120])]
    asyncio.run(run())


def test_never_overwrites_changed_self_consumption_session(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        device.mode = [1, 0xAA, 75, 0]
        device.heartbeat = [180]
        before = writes(device)
        assert not await controller.stop_dispatch()
        assert writes(device) == before
        assert device.mode == [1, 0xAA, 75, 0]
        assert device.heartbeat == [180]
    asyncio.run(run())


def test_never_overwrites_changed_external_heartbeat(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        device.heartbeat = [90]
        assert not await controller.stop_dispatch()
        assert device.mode == [1, 0xCC, 0, 0]
        assert device.heartbeat == [90]
    asyncio.run(run())


def test_below_device_minimum_is_rejected_without_writes(driver, device):
    async def run():
        device.data[3] = 20
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert not await controller.start_dispatch("discharge", 1000, 60)
        assert not writes(device)
    asyncio.run(run())


def test_ambiguous_dispatch_acknowledgement_restores_and_retains_retry_state(driver, device, monkeypatch):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        write = controller._write
        async def ambiguous_write(address, words, **kwargs):
            result = await write(address, words, **kwargs)
            return False if address == 8023 and words[0] == 4 else result
        monkeypatch.setattr(controller, "_write", ambiguous_write)
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert device.mode == [1, 0xCC, 0, 0]
        assert device.heartbeat == [600]
        assert not controller._owns_dispatch
        assert controller._task is None
    asyncio.run(run())


def test_permission_revoked_between_heartbeat_and_dispatch_blocks_vpp(driver, device, monkeypatch):
    async def run():
        allowed = True
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: allowed)
        heartbeat = controller._heartbeat
        async def revoke():
            nonlocal allowed
            result = await heartbeat()
            allowed = False
            return result
        monkeypatch.setattr(controller, "_heartbeat", revoke)
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert not any(call[1] == 8023 for call in writes(device))
        assert device.heartbeat == [600]
    asyncio.run(run())


def test_cancellation_during_initial_mode_readback_restores(driver, device, monkeypatch):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        read = controller._read
        async def cancelled_read(address, count, **kwargs):
            if address == 8023 and device.mode[0] == 4:
                monkeypatch.setattr(controller, "_read", read)
                raise asyncio.CancelledError
            return await read(address, count, **kwargs)
        monkeypatch.setattr(controller, "_read", cancelled_read)
        with pytest.raises(asyncio.CancelledError):
            await controller.start_dispatch("discharge", 5000, 60)
        assert device.mode == [1, 0xCC, 0, 0]
        assert device.heartbeat == [600]
        assert device.open_count == 0
    asyncio.run(run())


def test_heartbeat_is_renewed_only_while_owned_and_authorized(driver, device, monkeypatch):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        controller.RENEW_SECONDS = 0.001
        renewed = asyncio.Event()
        write = controller._write
        count = 0
        async def track_write(address, words, **kwargs):
            nonlocal count
            result = await write(address, words, **kwargs)
            if address == 8032 and words == [20]:
                count += 1
                if count == 2:
                    renewed.set()
            return result
        monkeypatch.setattr(controller, "_write", track_write)
        assert await controller.start_dispatch("discharge", 5000, 60)
        await asyncio.wait_for(renewed.wait(), timeout=1)
        assert device.mode == [4, 0xBB, 50, 0]
        assert await controller.stop_dispatch()
        assert device.heartbeat == [600]
    asyncio.run(run())


def test_expiry_during_snapshot_read_does_not_extend_lease(driver, device, monkeypatch):
    async def run():
        now = 100
        monkeypatch.setattr(driver, "time", SimpleNamespace(monotonic=lambda: now))
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        controller.RENEW_SECONDS = 0.001
        assert await controller.start_dispatch("discharge", 5000, 60)
        snapshot = controller._snapshot
        async def slow_snapshot():
            nonlocal now
            result = await snapshot()
            now = 161
            return result
        monkeypatch.setattr(controller, "_snapshot", slow_snapshot)
        await asyncio.wait_for(controller._task, timeout=1)
        assert len([call for call in writes(device) if call[1:] == (8032, [20])]) == 1
        assert device.mode == [1, 0xCC, 0, 0]
    asyncio.run(run())


def test_heartbeat_restore_failure_remains_retryable_after_mode_is_safe(driver, device, monkeypatch):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        assert await controller.start_dispatch("discharge", 5000, 60)
        write = controller._write
        async def fail_timeout_restore(address, words, **kwargs):
            if address == 8032 and words == [600]:
                return False
            return await write(address, words, **kwargs)
        monkeypatch.setattr(controller, "_write", fail_timeout_restore)
        assert not await controller.stop_dispatch()
        assert device.mode == [1, 0xCC, 0, 0]
        assert controller._heartbeat_owned
        assert not await controller.start_dispatch("charge", 5000, 60)
        monkeypatch.setattr(controller, "_write", write)
        assert await controller.stop_dispatch()
        assert device.heartbeat == [600]
    asyncio.run(run())


def test_read_only_probe_reports_native_capture_without_enabling_control(driver, device, monkeypatch):
    async def run():
        path = Path(__file__).resolve().parents[1] / "scripts/probe_ihomemanager.py"
        spec = importlib.util.spec_from_file_location("ihomemanager_probe_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(module, "load_driver", lambda: driver.SungrowIHomeManagerController)
        result = await module.probe("ihm.test", 503)
        assert result["control_enabled"] is False
        assert result["unit"] == 247
        assert result["protocol_version_hex"] == "0x01000200"
        assert result["native"]["raw_grid_power_w"] == -1000
        assert "ihm.test" not in str(result)
        assert not writes(device)
    asyncio.run(run())


@pytest.mark.parametrize("address,words", [(13049, [2]), (8048, [0xAA]), (8027, [170]), (8023, [5, 0xAA, 50, 0]), (8023, [4, 0xCC, 0, 0]), (8032, [0])])
def test_write_surface_rejects_unrelated_or_unsafe_registers(driver, device, address, words):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True)
        with pytest.raises(ValueError):
            await controller._write(address, words)
        assert not writes(device)
    asyncio.run(run())


def test_full_software_reserve_blocks_discharge_but_still_allows_charging(driver, device):
    async def run():
        controller = driver.SungrowIHomeManagerController("ihm.test", control_allowed=lambda: True, reserve_percent=100)
        assert not await controller.start_dispatch("discharge", 5000, 60)
        assert not writes(device)
        assert await controller.start_dispatch("charge", 5000, 60)
        assert await controller.async_shutdown()
    asyncio.run(run())
