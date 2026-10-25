"""Native iHomeManager battery dispatch, independent of inverter forwarding.

Implements Sungrow iHM protocol V1.0.2, not the SH inverter register map.
The default client is read-only. Control callers must explicitly supply a live
permission callback (Monitoring Mode, entry lifetime and external ownership).
This driver is deliberately not advertised as an optimizer connection until
native telemetry direction and the inverter telemetry source are validated.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import inspect
import logging
import math
import time
from typing import Callable

from pymodbus.client import AsyncModbusTcpClient

_LOGGER = logging.getLogger(__name__)
# pymodbus renamed the unit keyword. Inspect the API rather than a version string.
_UNIT_PARAMETER = (
    "device_id"
    if "device_id" in inspect.signature(AsyncModbusTcpClient.read_input_registers).parameters
    else "slave"
)


@dataclass(frozen=True)
class IHomeManagerSnapshot:
    """Raw system data; signed power direction is intentionally not inferred."""

    device_type: int
    protocol_number: int
    protocol_version: int
    min_charge_w: int
    max_charge_w: int
    min_discharge_w: int
    max_discharge_w: int
    soc_percent: float
    raw_inverter_power_w: int
    raw_grid_power_w: int
    raw_load_power_w: int
    raw_battery_power_w: int

    @property
    def supports_dispatch(self) -> bool:
        """Only V1.0.2 has a verified native VPP timeout contract here."""
        return self.device_type == 0x072A and self.protocol_version == 0x01000200


class SungrowIHomeManagerController:
    """Opt-in, leased native VPP battery dispatch at Modbus unit 247.

    Each transaction closes the socket: iHM permits one connection per TCP port.
    A command acknowledgement is insufficient; mode, direction, target and
    heartbeat must read back before success is returned. No PV generation or
    normalized battery/grid direction is invented from native signed readings.
    """

    UNIT = 247
    HEARTBEAT_SECONDS = 20
    RENEW_SECONDS = 10
    MAX_DURATION_SECONDS = 1800
    MODE_ADDRESS = 8023
    HEARTBEAT_ADDRESS = 8032
    MODE_SELF_CONSUMPTION = 1
    MODE_VPP = 4
    CMD_CHARGE = 0xAA
    CMD_DISCHARGE = 0xBB
    CMD_STOP = 0xCC
    _port_locks: dict[tuple[str, int], asyncio.Lock] = {}

    def __init__(
        self,
        host: str,
        port: int = 503,
        *,
        control_allowed: Callable[[], bool] | None = None,
        reserve_percent: float = 20,
        max_soc_percent: float = 100,
    ) -> None:
        if not host.strip() or int(port) not in (503, 504):
            raise ValueError("Use a configured plaintext iHomeManager port 503 or 504")
        if not (0 <= reserve_percent < max_soc_percent <= 100):
            raise ValueError("Invalid battery SOC boundaries")
        self.host = host.strip()
        self.port = int(port)
        self._control_allowed = control_allowed
        self._reserve_percent = reserve_percent
        self._max_soc_percent = max_soc_percent
        self._command_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._port_lock = self._port_locks.setdefault((self.host, self.port), asyncio.Lock())
        self._client = None
        self._task: asyncio.Task | None = None
        self._expected: list[int] | None = None
        self._owns_dispatch = False
        self._baseline: list[int] | None = None
        self._original_heartbeat: int | None = None
        self._heartbeat_owned = False
        self._deadline = 0.0
        self._closed = False

    def _allowed(self) -> bool:
        try:
            return not self._closed and self._control_allowed is not None and self._control_allowed() is True
        except Exception:
            return False

    @asynccontextmanager
    async def _transaction(self):
        async with self._port_lock:
            client = AsyncModbusTcpClient(self.host, port=self.port, timeout=3, retries=0)
            self._client = client
            try:
                if not await client.connect():
                    raise ConnectionError("iHomeManager Modbus connection failed")
                yield
            finally:
                client.close()
                self._client = None

    async def _read(self, address: int, count: int, *, holding: bool = False) -> list[int]:
        method = self._client.read_holding_registers if holding else self._client.read_input_registers
        response = await method(address=address, count=count, **{_UNIT_PARAMETER: self.UNIT})
        if response.isError() or len(response.registers) != count:
            raise ValueError(f"Invalid iHomeManager register response at {address}")
        words = response.registers
        if any(isinstance(word, bool) or not isinstance(word, int) or not 0 <= word <= 0xFFFF for word in words):
            raise ValueError("Invalid Modbus word")
        return words

    async def _write(self, address: int, words: list[int], *, restore: bool = False) -> bool:
        # No SH inverter, charger, feed-in limit, or power-on/off writes exist here.
        if not (
            (address == self.MODE_ADDRESS and (
                (len(words) == 4 and words[0] == self.MODE_VPP
                 and words[1] in (self.CMD_CHARGE, self.CMD_DISCHARGE)
                 and all(type(word) is int and 0 <= word <= 0xFFFF for word in words)
                 and (words[2] or words[3]))
                or (restore and words == [self.MODE_SELF_CONSUMPTION, self.CMD_STOP, 0, 0])
            ))
            or (address == self.HEARTBEAT_ADDRESS and (
                words == [self.HEARTBEAT_SECONDS]
                or (restore and self._heartbeat_owned and words == [self._original_heartbeat])
            ))
        ):
            raise ValueError("Unsupported iHomeManager write")
        if not restore and (address == self.HEARTBEAT_ADDRESS or words[0] == self.MODE_VPP) and not self._allowed():
            return False
        if (
            not restore and address == self.HEARTBEAT_ADDRESS
            and self._owns_dispatch and time.monotonic() >= self._deadline
        ):
            return False
        response = await self._client.write_registers(
            address=address, values=words, **{_UNIT_PARAMETER: self.UNIT}
        )
        return not response.isError()

    @staticmethod
    def _u32(words: list[int]) -> int:
        value = words[0] | (words[1] << 16)
        if value == 0xFFFFFFFF:
            raise ValueError("Unsupported unsigned iHomeManager register")
        return value

    @classmethod
    def _s32(cls, words: list[int]) -> int:
        value = words[0] | (words[1] << 16)
        if value == 0x7FFFFFFF:
            raise ValueError("Unsupported signed iHomeManager register")
        return value - 0x100000000 if value & 0x80000000 else value

    async def _snapshot(self) -> IHomeManagerSnapshot:
        identity = await self._read(7999, 5)  # 8000..8004, FC04
        if identity[0] != 0x072A:
            raise ValueError("Endpoint is not a Sungrow iHomeManager")
        data = await self._read(8150, 13)  # 8151..8163, FC04
        if any(data[index] == 0xFFFF for index in (0, 1, 2, 3, 12)) or data[12] > 1000:
            raise ValueError("Invalid iHomeManager battery limits or SOC")
        return IHomeManagerSnapshot(
            device_type=identity[0], protocol_number=self._u32(identity[1:3]),
            protocol_version=self._u32(identity[3:5]),
            min_charge_w=data[1] * 100, max_charge_w=data[0] * 100,
            min_discharge_w=data[3] * 100, max_discharge_w=data[2] * 100,
            soc_percent=data[12] / 10,
            raw_inverter_power_w=self._s32(data[4:6]) * 10,
            raw_grid_power_w=self._s32(data[6:8]) * 10,
            raw_load_power_w=self._s32(data[8:10]) * 10,
            raw_battery_power_w=self._s32(data[10:12]) * 10,
        )

    async def read_snapshot(self) -> IHomeManagerSnapshot:
        """Read identity, limits, SOC and raw power without sending any writes."""
        async with self._lock, self._transaction():
            return await self._snapshot()

    def _within_limits(self, snapshot: IHomeManagerSnapshot, command: int, power_w: int) -> bool:
        if not snapshot.supports_dispatch:
            return False
        if command == self.CMD_CHARGE:
            return snapshot.soc_percent < self._max_soc_percent and 0 < power_w <= snapshot.max_charge_w and power_w >= snapshot.min_charge_w
        return snapshot.soc_percent > self._reserve_percent and 0 < power_w <= snapshot.max_discharge_w and power_w >= snapshot.min_discharge_w

    async def start_dispatch(self, action: str, power_w: float, duration_seconds: float) -> bool:
        """Serialize dispatch replacement, shutdown and explicit cancellation."""
        async with self._command_lock:
            return await self._start_dispatch(action, power_w, duration_seconds)

    async def _start_dispatch(self, action: str, power_w: float, duration_seconds: float) -> bool:
        """Dispatch charge/discharge for a finite lease, then restore self-consumption.

        The caller owns planning, fresh normalized telemetry and user intent.
        This target is battery power, not guaranteed grid export. The existing
        native site export cap and battery reserve registers are never changed.
        """
        if action not in ("charge", "discharge"):
            raise ValueError("Action must be charge or discharge")
        if (
            isinstance(power_w, bool) or not isinstance(power_w, (int, float))
            or not math.isfinite(power_w) or power_w < 100
            or isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= self.MAX_DURATION_SECONDS
        ):
            raise ValueError("Supply positive power >=100 W and a duration up to 1800 seconds")
        if not self._allowed():
            return False
        # Stop the prior lease before changing targets. Failed restoration stays
        # owned and retryable; a new command must not discard its cleanup state.
        if not await self._stop_dispatch():
            return False
        command = self.CMD_CHARGE if action == "charge" else self.CMD_DISCHARGE
        async with self._lock:
            if not self._allowed():
                return False
            try:
                async with self._transaction():
                    snapshot = await self._snapshot()
                    limit_w = snapshot.max_charge_w if action == "charge" else snapshot.max_discharge_w
                    # Round down so quantization cannot exceed requested/BMS power.
                    target_units = int(min(power_w, limit_w) // 100)
                    if not self._within_limits(snapshot, command, target_units * 100):
                        return False
                    current = await self._read(self.MODE_ADDRESS, 4, holding=True)
                    if current[0] != self.MODE_SELF_CONSUMPTION:
                        return False  # Never take over somebody else's VPP/compulsory session.
                    if not self._allowed():
                        return False
                    previous_heartbeat = await self._read(self.HEARTBEAT_ADDRESS, 1, holding=True)
                    if not 0 <= previous_heartbeat[0] <= 1000:
                        return False
                    self._baseline = current[:]
                    self._original_heartbeat = previous_heartbeat[0]
                    self._heartbeat_owned = True
                    if not await self._heartbeat():
                        await self._release()
                        return False
                    self._expected = [self.MODE_VPP, command, target_units & 0xFFFF, target_units >> 16]
                    # Ownership starts before an ambiguous write acknowledgement.
                    self._owns_dispatch = True
                    if not await self._write(self.MODE_ADDRESS, self._expected):
                        await self._release()
                        return False
                    if await self._read(self.MODE_ADDRESS, 4, holding=True) != self._expected or not self._allowed():
                        await self._release()
                        return False
                self._deadline = time.monotonic() + duration_seconds
                self._task = asyncio.create_task(self._renew(), name="power_sync_ihomemanager_dispatch")
                return True
            except asyncio.CancelledError:
                await self._recover_after_error()
                raise
            except Exception as err:
                _LOGGER.warning("iHomeManager dispatch failed: %s", err)
                await self._recover_after_error()
                return False

    async def _heartbeat(self) -> bool:
        if not self._allowed():
            return False
        return (
            await self._write(self.HEARTBEAT_ADDRESS, [self.HEARTBEAT_SECONDS])
            and await self._read(self.HEARTBEAT_ADDRESS, 1, holding=True) == [self.HEARTBEAT_SECONDS]
        )

    async def _release(self) -> bool:
        target = [self.MODE_SELF_CONSUMPTION, self.CMD_STOP, 0, 0]
        if self._owns_dispatch:
            current = await self._read(self.MODE_ADDRESS, 4, holding=True)
            if current not in (self._expected, self._baseline, target):
                # The protocol has no owner token. A different block is positive
                # evidence of an ownership change, including in self-consumption.
                # Do not alter that session's timeout either.
                _LOGGER.warning("iHomeManager dispatch ownership changed; leaving external settings unchanged")
                self._owns_dispatch = False
                self._heartbeat_owned = False
                self._expected = None
                self._baseline = None
                self._original_heartbeat = None
                return False
            if current == self._expected:
                if not await self._write(self.MODE_ADDRESS, target, restore=True):
                    return False
                if await self._read(self.MODE_ADDRESS, 4, holding=True) != target:
                    return False
            self._owns_dispatch = False
            self._expected = None
            self._baseline = None
        if self._heartbeat_owned:
            current = await self._read(self.HEARTBEAT_ADDRESS, 1, holding=True)
            if current not in ([self.HEARTBEAT_SECONDS], [self._original_heartbeat]):
                self._heartbeat_owned = False
                self._original_heartbeat = None
                return False
            if current != [self._original_heartbeat]:
                if not await self._write(self.HEARTBEAT_ADDRESS, [self._original_heartbeat], restore=True):
                    return False
                if await self._read(self.HEARTBEAT_ADDRESS, 1, holding=True) != [self._original_heartbeat]:
                    return False
            self._heartbeat_owned = False
            self._original_heartbeat = None
        return True

    async def _recover_after_error(self) -> bool:
        if not self._owns_dispatch and not self._heartbeat_owned:
            return True
        try:
            async with self._transaction():
                return await self._release()
        except Exception as err:
            _LOGGER.warning("iHomeManager restore unconfirmed; heartbeat renewal stopped: %s", err)
            return False

    async def _renew(self) -> None:
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(self.RENEW_SECONDS, remaining))
                async with self._lock:
                    if time.monotonic() >= self._deadline or not self._allowed():
                        break
                    async with self._transaction():
                        if await self._read(self.MODE_ADDRESS, 4, holding=True) != self._expected:
                            break
                        if await self._read(self.HEARTBEAT_ADDRESS, 1, holding=True) != [self.HEARTBEAT_SECONDS]:
                            break
                        snapshot = await self._snapshot()
                        power_w = (self._expected[2] | (self._expected[3] << 16)) * 100
                        if (
                            time.monotonic() >= self._deadline
                            or not self._within_limits(snapshot, self._expected[1], power_w)
                            or not await self._heartbeat()
                        ):
                            break
        except asyncio.CancelledError:
            # stop_dispatch waits for cancellation before restoring under lock.
            raise
        except Exception as err:
            _LOGGER.warning("iHomeManager heartbeat renewal stopped: %s", err)
        async with self._lock:
            await self._recover_after_error()

    async def stop_dispatch(self) -> bool:
        """Serialize stop with command replacement."""
        async with self._command_lock:
            return await self._stop_dispatch()

    async def _stop_dispatch(self) -> bool:
        """Stop renewal and restore only this client's owned dispatch session."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            return await self._recover_after_error()

    async def async_shutdown(self) -> bool:
        """Prevent new dispatch, stop heartbeat and restore on unload."""
        self._closed = True
        return await self.stop_dispatch()
