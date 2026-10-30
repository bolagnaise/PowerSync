#!/usr/bin/env python3
"""Bounded, fail-closed GoodWe EHA-G20 native load-following experiment.

The default ``inspect`` command is read-only.  ``run`` is deliberately hard to
invoke and writes only EMSPowerMode (47511) and EMSPowerSet (47512).
Write acknowledgement and readback do not prove physical load-following.

The tool is intended for Linux/macOS (``fcntl.flock`` is used for an
endpoint-global inter-process lock).  SIGINT and SIGTERM request a clean
restore; a second signal during restore is ignored so it cannot interrupt the
recovery sequence.  SIGKILL and host power loss cannot guarantee restoration;
use ``recover`` with the durable journal in that case.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import errno
import fcntl
import hashlib
import ipaddress
import json
import math
import os
import signal
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


TOOL_VERSION = "0.1.0-beta.1"
MODEL_ADDRESS = 35060
MODEL_COUNT = 16
SERIAL_ADDRESS = 35003
SERIAL_COUNT = 8
WORK_MODE_ADDRESS = 35187
EMS_MODE_ADDRESS = 47511
EMS_TARGET_ADDRESS = 47512
FALLBACK_ADDRESS = 47505
EXPECTED_MODEL = "GW9.999K-EHA-G20"
SELF_USE_MODE = 1
BUY_POWER_MODE = 9
ALLOWED_FALLBACKS = (3, 10)
JOURNAL_SCHEMA = 1
ADDRESSES = {
    "ems_mode": EMS_MODE_ADDRESS,
    "ems_target": EMS_TARGET_ADDRESS,
    "work_mode": WORK_MODE_ADDRESS,
    "fallback": FALLBACK_ADDRESS,
}
TELEMETRY_LAYOUT = {
    "backup_power_w": (35169, 2, "s32"),
    "on_grid_load_power_w": (35171, 2, "s32"),
    "battery_power_w": (35182, 2, "s32"),
    "pv_power_w": (35301, 2, "u32"),
    "meter_power_w": (36025, 2, "s32"),
    "soc_percent": (37007, 1, "u16"),
}


class TrialError(RuntimeError):
    """Expected, safety-relevant trial failure."""


class ProtocolError(TrialError):
    """Malformed, mismatched, or exceptional Modbus/TCP response."""


class LockBusy(TrialError):
    """Another instance owns this endpoint."""


class RestoreRefused(TrialError):
    """Recovery would overwrite state the trial no longer owns."""


@dataclasses.dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    unit: int

    def public(self) -> Dict[str, Any]:
        return {"host": self.host, "port": self.port, "unit": self.unit}

    def key(self) -> str:
        raw = f"{self.host}\0{self.port}\0{self.unit}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:24]


class ModbusTcpClient:
    """Small one-request-per-connection Modbus/TCP client."""

    def __init__(self, endpoint: Endpoint, timeout: float = 1.5) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._txid = 0

    def _next_txid(self) -> int:
        self._txid = (self._txid % 0xFFFF) + 1
        return self._txid

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
        chunks: List[bytes] = []
        remaining = size
        while remaining:
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                raise TrialError("Modbus/TCP response exceeded total request timeout")
            sock.settimeout(time_left)
            chunk = sock.recv(remaining)
            if not chunk:
                raise ProtocolError("connection closed before complete response")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _request(self, pdu: bytes) -> bytes:
        txid = self._next_txid()
        header = struct.pack(">HHHB", txid, 0, len(pdu) + 1, self.endpoint.unit)
        deadline = time.monotonic() + self.timeout
        try:
            with socket.create_connection(
                (self.endpoint.host, self.endpoint.port), timeout=self.timeout
            ) as sock:
                time_left = deadline - time.monotonic()
                if time_left <= 0:
                    raise TrialError("Modbus/TCP connection exceeded total request timeout")
                sock.settimeout(time_left)
                sock.sendall(header + pdu)
                response_header = self._recv_exact(sock, 7, deadline)
                rx_txid, protocol, length, unit = struct.unpack(">HHHB", response_header)
                if rx_txid != txid:
                    raise ProtocolError("transaction identifier mismatch")
                if protocol != 0:
                    raise ProtocolError("non-zero Modbus protocol identifier")
                if unit != self.endpoint.unit:
                    raise ProtocolError("unit identifier mismatch")
                if length < 2 or length > 260:
                    raise ProtocolError("invalid MBAP length")
                response_pdu = self._recv_exact(sock, length - 1, deadline)
        except (OSError, socket.timeout) as exc:
            raise TrialError(f"Modbus/TCP communication failed: {exc}") from exc

        if not response_pdu:
            raise ProtocolError("empty Modbus PDU")
        function = response_pdu[0]
        expected = pdu[0]
        if function == (expected | 0x80):
            if len(response_pdu) != 2:
                raise ProtocolError("malformed Modbus exception response")
            raise ProtocolError(f"Modbus exception code {response_pdu[1]}")
        if function != expected:
            raise ProtocolError("function code mismatch")
        return response_pdu

    def read_holding(self, address: int, count: int) -> List[int]:
        if not (0 <= address <= 0xFFFF and 1 <= count <= 125 and address + count <= 0x10000):
            raise ValueError("invalid register read")
        pdu = struct.pack(">BHH", 3, address, count)
        response = self._request(pdu)
        expected_bytes = count * 2
        if len(response) != expected_bytes + 2 or response[1] != expected_bytes:
            raise ProtocolError("FC03 byte count/length mismatch")
        return list(struct.unpack(f">{count}H", response[2:]))

    def write_single(self, address: int, value: int) -> None:
        if not (0 <= address <= 0xFFFF and 0 <= value <= 0xFFFF):
            raise ValueError("invalid single-register write")
        pdu = struct.pack(">BHH", 6, address, value)
        response = self._request(pdu)
        if response != pdu:
            raise ProtocolError("FC06 echo mismatch")

    def write_multiple(self, address: int, values: Sequence[int]) -> None:
        if not values or len(values) > 123:
            raise ValueError("invalid multiple-register write")
        if (
            not 0 <= address <= 0xFFFF
            or address + len(values) > 0x10000
            or any(not 0 <= value <= 0xFFFF for value in values)
        ):
            raise ValueError("invalid multiple-register write")
        payload = struct.pack(f">{len(values)}H", *values)
        pdu = struct.pack(">BHHB", 16, address, len(values), len(payload)) + payload
        response = self._request(pdu)
        expected = struct.pack(">BHH", 16, address, len(values))
        if response != expected:
            raise ProtocolError("FC16 echo mismatch")


def _words_to_bytes(words: Sequence[int]) -> bytes:
    return struct.pack(f">{len(words)}H", *words)


def _ascii_registers(words: Sequence[int]) -> str:
    raw = _words_to_bytes(words).split(b"\0", 1)[0]
    try:
        return raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise TrialError("identity register contains non-ASCII data") from exc


def _u32(words: Sequence[int]) -> int:
    if len(words) != 2:
        raise ValueError("U32 requires two words")
    return (words[0] << 16) | words[1]


def _s32(words: Sequence[int]) -> int:
    value = _u32(words)
    return value - 0x100000000 if value & 0x80000000 else value


def read_identity(client: Any) -> Tuple[str, str]:
    model_words = client.read_holding(MODEL_ADDRESS, MODEL_COUNT)
    serial_words = client.read_holding(SERIAL_ADDRESS, SERIAL_COUNT)
    model = _ascii_registers(model_words)
    digest = hashlib.sha256(
        _words_to_bytes(model_words) + b"\0" + _words_to_bytes(serial_words)
    ).hexdigest()
    return model, digest


def read_snapshot(client: Any, clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    started = clock()
    snapshot: Dict[str, Any] = {
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "units": "W raw; meter sign convention unverified",
    }
    for name, (address, count, kind) in TELEMETRY_LAYOUT.items():
        words = client.read_holding(address, count)
        if kind == "u16":
            value = words[0]
        elif kind == "u32":
            value = _u32(words)
        else:
            value = _s32(words)
        snapshot[name] = value
    snapshot["work_mode"] = client.read_holding(WORK_MODE_ADDRESS, 1)[0]
    controls = client.read_holding(47509, 4)
    snapshot["export_limit_enabled"] = controls[0]
    snapshot["export_limit_w_raw"] = controls[1] - 0x10000 if controls[1] & 0x8000 else controls[1]
    snapshot["ems_mode"] = controls[2]
    snapshot["ems_target_w_raw"] = controls[3]
    elapsed = clock() - started
    if elapsed < 0 or elapsed > 2.0:
        raise TrialError(f"telemetry sample exceeded the 2-second freshness budget ({elapsed:.3f}s)")
    snapshot["sample_duration_s"] = round(elapsed, 6)
    return snapshot


def read_fallback(client: Any) -> int:
    return client.read_holding(FALLBACK_ADDRESS, 1)[0]


def endpoint_lock_path(endpoint: Endpoint) -> Path:
    return Path(tempfile.gettempdir()) / f"goodwe-load-following-{endpoint.key()}.lock"


@contextlib.contextmanager
def endpoint_lock(endpoint: Endpoint) -> Iterable[None]:
    path = endpoint_lock_path(endpoint)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LockBusy(f"another trial process owns endpoint {endpoint.host}:{endpoint.port}/{endpoint.unit}") from exc
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value: Dict[str, Any], *, create_only: bool = False) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if create_only and path.exists():
        raise TrialError(f"journal already exists; choose a fresh --journal path: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if create_only:
            try:
                os.link(str(temp_path), str(path))
            except FileExistsError as exc:
                raise TrialError(f"journal already exists; choose a fresh --journal path: {path}") from exc
            temp_path.unlink()
        else:
            os.replace(str(temp_path), str(path))
        _fsync_directory(path.parent)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _validate_target(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFF:
        raise TrialError("journal original target is invalid")
    return value


def validate_journal(data: Any, endpoint: Endpoint) -> Dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != JOURNAL_SCHEMA:
        raise TrialError("unsupported or invalid journal schema")
    if data.get("endpoint") != endpoint.public():
        raise TrialError("journal endpoint does not match requested endpoint")
    if data.get("addresses") != ADDRESSES:
        raise TrialError("journal register map is invalid or tampered")
    if data.get("expected_model") != EXPECTED_MODEL:
        raise TrialError("journal model constraint is invalid")
    identity_hash = data.get("identity_hash")
    if not isinstance(identity_hash, str) or len(identity_hash) != 64:
        raise TrialError("journal identity hash is invalid")
    snapshot = data.get("original")
    if not isinstance(snapshot, dict) or snapshot.get("work_mode") != 1 or snapshot.get("ems_mode") != 1:
        raise TrialError("journal original modes are invalid")
    _validate_target(snapshot.get("target"))
    fallback = snapshot.get("fallback")
    if fallback not in ALLOWED_FALLBACKS:
        raise TrialError("journal fallback is invalid")
    if data.get("state") not in {"pending", "active", "recovery_required", "restored"}:
        raise TrialError("journal state is invalid")
    return data


def load_journal(path: Path, endpoint: Endpoint) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise TrialError(f"cannot read journal: {exc}") from exc
    return validate_journal(data, endpoint)


class SignalGuard:
    def __init__(self) -> None:
        self.requested: Optional[int] = None
        self.restoring = False
        self.repeated_during_restore = False
        self._old: Dict[int, Any] = {}

    def _handler(self, signum: int, _frame: Any) -> None:
        if self.restoring:
            self.repeated_during_restore = True
            return
        if self.requested is None:
            self.requested = signum

    def __enter__(self) -> "SignalGuard":
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._old[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handler)
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        for signum, old in self._old.items():
            signal.signal(signum, old)


def _assert_owned_snapshot(
    snapshot: Dict[str, Any],
    expected_mode: int,
    expected_target: Optional[int] = None,
    expected_export: Optional[Tuple[int, int]] = None,
    minimum_soc: int = 0,
) -> None:
    if snapshot["work_mode"] != 1:
        raise TrialError(f"work mode changed from on-grid mode 1 to {snapshot['work_mode']}")
    if snapshot["ems_mode"] != expected_mode:
        raise TrialError(f"EMS mode changed from expected {expected_mode} to {snapshot['ems_mode']}")
    if expected_target is not None and snapshot["ems_target_w_raw"] != expected_target:
        raise TrialError("EMS target changed from the trial's zero target")
    if expected_export is not None and (
        snapshot["export_limit_enabled"], snapshot["export_limit_w_raw"]
    ) != expected_export:
        raise TrialError("export-limit settings changed during the trial")
    soc = snapshot["soc_percent"]
    if not 0 <= soc <= 100:
        raise TrialError(f"invalid battery SOC {soc}")
    if soc < minimum_soc:
        raise TrialError(f"battery SOC {soc}% is below the {minimum_soc}% trial floor")


def write_trial_mode(client: Any) -> None:
    client.write_multiple(EMS_TARGET_ADDRESS, [0])
    client.write_multiple(EMS_MODE_ADDRESS, [BUY_POWER_MODE])


def verify_registers(client: Any, mode: int, target: int) -> None:
    values = client.read_holding(EMS_MODE_ADDRESS, 2)
    if values != [mode, target]:
        raise TrialError(
            f"control readback mismatch: expected mode {mode} and target {target}, got {values}"
        )


def restore_once(client: Any, journal: Dict[str, Any]) -> None:
    original_target = _validate_target(journal["original"]["target"])
    current = client.read_holding(EMS_MODE_ADDRESS, 2)
    current_mode, current_target = current
    if current_mode not in (SELF_USE_MODE, BUY_POWER_MODE):
        raise RestoreRefused(f"foreign EMS mode {current_mode}; recovery will not overwrite it")
    if current_target not in (original_target, 0):
        raise RestoreRefused("foreign EMS target; recovery will not overwrite it")
    if current_mode != SELF_USE_MODE:
        client.write_multiple(EMS_MODE_ADDRESS, [SELF_USE_MODE])
        if client.read_holding(EMS_MODE_ADDRESS, 1)[0] != SELF_USE_MODE:
            raise TrialError("restored EMS mode did not read back as self-use mode 1")
    if current_target != original_target:
        client.write_multiple(EMS_TARGET_ADDRESS, [original_target])
    verify_registers(client, SELF_USE_MODE, original_target)


def restore_with_retries(
    client: Any,
    journal: Dict[str, Any],
    journal_path: Path,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    last_error: Optional[BaseException] = None
    for attempt in range(1, 4):
        try:
            model, identity_hash = read_identity(client)
            if model != EXPECTED_MODEL or identity_hash != journal["identity_hash"]:
                raise RestoreRefused("live inverter identity does not match the recovery journal")
            restore_once(client, journal)
            journal["state"] = "restored"
            journal["restored_fields"] = ["ems_mode", "ems_target"]
            journal["restored_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            journal["restore_attempts"] = attempt
            journal.pop("last_restore_error", None)
            atomic_write_json(journal_path, journal)
            return
        except RestoreRefused:
            journal["state"] = "recovery_required"
            journal["last_restore_error"] = str(sys.exc_info()[1])
            atomic_write_json(journal_path, journal)
            raise
        except (TrialError, OSError) as exc:
            last_error = exc
            if attempt < 3:
                sleeper(0.2)
    journal["state"] = "recovery_required"
    journal["last_restore_error"] = str(last_error)
    journal["restore_attempts"] = 3
    atomic_write_json(journal_path, journal)
    raise TrialError(f"restoration was not confirmed after 3 attempts: {last_error}")


def _journal_for(
    endpoint: Endpoint,
    identity_hash: str,
    snapshot: Dict[str, Any],
    fallback: int,
) -> Dict[str, Any]:
    return {
        "schema": JOURNAL_SCHEMA,
        "tool_version": TOOL_VERSION,
        "state": "pending",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "endpoint": endpoint.public(),
        "expected_model": EXPECTED_MODEL,
        "identity_hash": identity_hash,
        "addresses": dict(ADDRESSES),
        "original": {
            "work_mode": snapshot["work_mode"],
            "ems_mode": snapshot["ems_mode"],
            "target": snapshot["ems_target_w_raw"],
            "fallback": fallback,
            "export_limit_enabled": snapshot["export_limit_enabled"],
            "export_limit_w_raw": snapshot["export_limit_w_raw"],
        },
    }


def collect_window(
    client: Any,
    phase: str,
    seconds: float,
    log_path: Optional[Path],
    expected_mode: int,
    minimum_soc: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] = lambda: False,
) -> List[Dict[str, Any]]:
    deadline = clock() + seconds
    samples: List[Dict[str, Any]] = []
    baseline_control: Optional[Tuple[int, int, int]] = None
    while True:
        if stop_requested():
            raise TrialError("termination requested")
        before = clock()
        if before >= deadline and samples:
            break
        snapshot = read_snapshot(client, clock)
        _assert_owned_snapshot(snapshot, expected_mode, minimum_soc=minimum_soc)
        current_control = (
            snapshot["ems_target_w_raw"],
            snapshot["export_limit_enabled"],
            snapshot["export_limit_w_raw"],
        )
        if baseline_control is None:
            baseline_control = current_control
        elif current_control != baseline_control:
            raise TrialError(f"{phase} control registers changed during the observation window")
        record = {"phase": phase, "telemetry": snapshot}
        if log_path is not None:
            append_jsonl(log_path, record)
        samples.append(snapshot)
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleeper(min(1.0, remaining))
    if not samples:
        raise TrialError(f"no complete {phase} telemetry sample")
    return samples


def run_trial(
    client: Any,
    endpoint: Endpoint,
    duration: int,
    journal_path: Path,
    log_path: Path,
    *,
    baseline_seconds: float = 5.0,
    after_seconds: float = 5.0,
    heartbeat_seconds: float = 0.8,
    sample_seconds: float = 5.0,
    minimum_soc: int = 95,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    signal_guard: Optional[SignalGuard] = None,
) -> None:
    if journal_path.exists():
        raise TrialError(f"journal already exists; choose a fresh --journal path: {journal_path}")
    if log_path.exists():
        raise TrialError(f"log already exists; choose a fresh --log path: {log_path}")
    model, identity_hash = read_identity(client)
    if model != EXPECTED_MODEL:
        raise TrialError(f"unsupported inverter model {model!r}; expected {EXPECTED_MODEL!r}")
    fallback = read_fallback(client)
    if fallback not in ALLOWED_FALLBACKS:
        if fallback == 2:
            raise TrialError("fallback mode 2 has a shutdown watchdog and is refused")
        raise TrialError(f"fallback mode {fallback} is unsupported; only 3 or 10 are allowed")

    append_jsonl(log_path, {
        "phase": "metadata",
        "tool_version": TOOL_VERSION,
        "model": model,
        "identity_hash": identity_hash,
        "fallback": fallback,
        "duration_seconds": duration,
        "minimum_soc_percent": minimum_soc,
        "ems_mode_register": EMS_MODE_ADDRESS,
        "ems_target_register": EMS_TARGET_ADDRESS,
        "requested_mode": BUY_POWER_MODE,
        "requested_target_w": 0,
        "physical_success_claimed": False,
    })

    baseline = collect_window(
        client,
        "before",
        baseline_seconds,
        log_path,
        SELF_USE_MODE,
        minimum_soc,
        clock,
        sleeper,
        (lambda: signal_guard.requested is not None) if signal_guard else (lambda: False),
    )
    original = baseline[-1]
    original_export = (original["export_limit_enabled"], original["export_limit_w_raw"])
    journal = _journal_for(endpoint, identity_hash, original, fallback)
    atomic_write_json(journal_path, journal, create_only=True)

    recovery_armed = True
    failure: Optional[BaseException] = None
    try:
        if signal_guard and signal_guard.requested is not None:
            raise TrialError(f"termination requested by signal {signal_guard.requested}")
        current = read_snapshot(client, clock)
        _assert_owned_snapshot(current, SELF_USE_MODE, minimum_soc=minimum_soc)
        current_fallback = read_fallback(client)
        if (
            current["ems_target_w_raw"] != original["ems_target_w_raw"]
            or (current["export_limit_enabled"], current["export_limit_w_raw"]) != original_export
            or current_fallback != fallback
        ):
            raise TrialError("control registers changed after baseline; refusing first write")
        if signal_guard and signal_guard.requested is not None:
            raise TrialError(f"termination requested by signal {signal_guard.requested}")
        write_trial_mode(client)
        start = clock()
        deadline = start + duration
        verify_registers(client, BUY_POWER_MODE, 0)
        journal["state"] = "active"
        atomic_write_json(journal_path, journal)

        last_heartbeat = start
        next_heartbeat = start + heartbeat_seconds
        next_sample = start
        while clock() < deadline:
            if signal_guard and signal_guard.requested is not None:
                raise TrialError(f"termination requested by signal {signal_guard.requested}")
            now = clock()
            if now >= next_heartbeat:
                if now - last_heartbeat > 1.0:
                    raise TrialError("heartbeat freshness budget exceeded")
                controls = client.read_holding(47509, 4)
                expected_controls = [
                    original_export[0],
                    original_export[1] & 0xFFFF,
                    BUY_POWER_MODE,
                    0,
                ]
                if controls != expected_controls:
                    raise TrialError(f"foreign EMS control detected before heartbeat: {controls}")
                if read_fallback(client) != fallback:
                    raise TrialError("fallback setting changed during the trial")
                if clock() - last_heartbeat > 1.0:
                    raise TrialError("heartbeat freshness budget exceeded during control verification")
                if signal_guard and signal_guard.requested is not None:
                    raise TrialError(f"termination requested by signal {signal_guard.requested}")
                client.write_multiple(EMS_MODE_ADDRESS, [BUY_POWER_MODE])
                last_heartbeat = clock()
                next_heartbeat = now + heartbeat_seconds
            if now >= next_sample:
                snapshot = read_snapshot(client, clock)
                _assert_owned_snapshot(
                    snapshot,
                    BUY_POWER_MODE,
                    expected_target=0,
                    expected_export=original_export,
                    minimum_soc=minimum_soc,
                )
                append_jsonl(log_path, {"phase": "active", "telemetry": snapshot})
                next_sample = now + sample_seconds
            sleep_for = min(deadline, next_heartbeat, next_sample) - clock()
            if sleep_for > 0:
                sleeper(sleep_for)
    except BaseException as exc:
        failure = exc
    finally:
        if recovery_armed:
            if signal_guard:
                signal_guard.restoring = True
            try:
                restore_with_retries(client, journal, journal_path, sleeper)
            except BaseException as restore_error:
                if failure is None:
                    failure = restore_error
                else:
                    failure = TrialError(f"trial failed ({failure}); restoration also failed ({restore_error})")

    if failure is not None:
        raise failure
    collect_window(
        client,
        "after",
        after_seconds,
        log_path,
        SELF_USE_MODE,
        0,
        clock,
        sleeper,
    )


def inspect_endpoint(client: Any, endpoint: Endpoint) -> Dict[str, Any]:
    model, identity_hash = read_identity(client)
    snapshot = read_snapshot(client)
    fallback = read_fallback(client)
    reasons: List[str] = []
    if model != EXPECTED_MODEL:
        reasons.append(f"model must be exactly {EXPECTED_MODEL}")
    if snapshot["work_mode"] != 1:
        reasons.append("work mode must be on-grid mode 1")
    if snapshot["ems_mode"] != 1:
        reasons.append("EMS mode must be self-use mode 1")
    if fallback not in ALLOWED_FALLBACKS:
        reasons.append("fallback must be 3 or 10; mode 2 shutdown watchdog is refused")
    soc = snapshot["soc_percent"]
    if not 0 <= soc <= 100:
        reasons.append("battery SOC telemetry must be between 0 and 100%")
    elif soc < 95:
        reasons.append("battery SOC must be at least the default 95% trial floor")
    return {
        "endpoint": endpoint.public(),
        "tool_version": TOOL_VERSION,
        "model": model,
        "identity_hash": identity_hash,
        "fallback": fallback,
        "telemetry": snapshot,
        "trial_ready": not reasons,
        "reasons": reasons,
        "physical_success_claimed": False,
    }


def recover(client: Any, endpoint: Endpoint, journal_path: Path) -> None:
    journal = load_journal(journal_path, endpoint)
    model, identity_hash = read_identity(client)
    if model != EXPECTED_MODEL or identity_hash != journal["identity_hash"]:
        raise TrialError("live inverter identity does not match the recovery journal")
    if journal["state"] == "restored":
        verify_registers(client, SELF_USE_MODE, journal["original"]["target"])
        return
    restore_with_retries(client, journal, journal_path)


def _port(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be 1..65535")
    return number


def _unit(value: str) -> int:
    number = int(value)
    if not 1 <= number <= 247:
        raise argparse.ArgumentTypeError("unit must be 1..247")
    return number


def _timeout(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0.05 <= number <= 2.0:
        raise argparse.ArgumentTypeError("timeout must be finite and between 0.05 and 2 seconds")
    return number


def _duration(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("duration must be an integer from 30 to 300") from exc
    if not 30 <= number <= 300:
        raise argparse.ArgumentTypeError("duration must be an integer from 30 to 300")
    return number


def _minimum_soc(value: str) -> int:
    number = int(value)
    if not 90 <= number <= 100:
        raise argparse.ArgumentTypeError("minimum SOC must be an integer from 90 to 100")
    return number


def _ip_address(value: str) -> str:
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError as exc:
        raise argparse.ArgumentTypeError("host must be a literal IPv4 or IPv6 address") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=TOOL_VERSION)
    parser.add_argument("--host", type=_ip_address, required=True, help="inverter IPv4/IPv6 address (required)")
    parser.add_argument("--port", type=_port, default=502)
    parser.add_argument("--unit", type=_unit, default=247)
    parser.add_argument("--timeout", type=_timeout, default=1.5)
    parser.add_argument(
        "--journal",
        type=Path,
        help="durable recovery journal; run requires a path that does not exist",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("inspect", help="read identity, safety gates, and telemetry; performs no writes")
    run_parser = subparsers.add_parser("run", help="run the bounded experimental trial")
    run_parser.add_argument("--duration", type=_duration, default=30)
    run_parser.add_argument("--minimum-soc", type=_minimum_soc, default=95)
    run_parser.add_argument("--log", type=Path, required=True, help="JSONL telemetry log")
    run_parser.add_argument("--execute", action="store_true")
    run_parser.add_argument("--exclusive-control", action="store_true")
    run_parser.add_argument("--whole-site-meter-confirmed", action="store_true")
    subparsers.add_parser("recover", help="restore from an existing recovery journal")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "inspect"
    endpoint = Endpoint(args.host, args.port, args.unit)
    if command in {"run", "recover"} and args.journal is None:
        parser.error(f"{command} requires --journal")
    if command == "run" and not (
        args.execute and args.exclusive_control and args.whole_site_meter_confirmed
    ):
        parser.error(
            "run requires --execute --exclusive-control --whole-site-meter-confirmed"
        )
    if command == "run" and args.journal.resolve() == args.log.resolve():
        parser.error("--journal and --log must be different paths")

    client = ModbusTcpClient(endpoint, args.timeout)
    try:
        with endpoint_lock(endpoint):
            if command == "inspect":
                print(json.dumps(inspect_endpoint(client, endpoint), indent=2, sort_keys=True))
            elif command == "recover":
                with SignalGuard() as guard:
                    guard.restoring = True
                    recover(client, endpoint, args.journal)
                print(json.dumps({"state": "restored", "physical_success_claimed": False}))
            else:
                with SignalGuard() as guard:
                    run_trial(
                        client,
                        endpoint,
                        args.duration,
                        args.journal,
                        args.log,
                        minimum_soc=args.minimum_soc,
                        signal_guard=guard,
                    )
                print(
                    json.dumps(
                        {
                            "state": "restored",
                            "log": str(args.log),
                            "physical_success_claimed": False,
                            "note": "register acknowledgement/readback is not physical load-following proof",
                        }
                    )
                )
    except (TrialError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
