from __future__ import annotations

import importlib.util
import contextlib
import json
import socket
import struct
import sys
import threading
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "goodwe_load_following_trial.py"
SPEC = importlib.util.spec_from_file_location("goodwe_trial", SCRIPT)
trial = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = trial
SPEC.loader.exec_module(trial)


def ascii_words(text: str, count: int):
    raw = text.encode("ascii").ljust(count * 2, b"\0")
    return list(struct.unpack(f">{count}H", raw))


class FakeClient:
    def __init__(self):
        self.model = trial.EXPECTED_MODEL
        self.serial = "SERIAL-SECRET"
        self.fallback = 10
        self.work_mode = 1
        self.ems_mode = 1
        self.target = 321
        self.export_enabled = 0
        self.export_limit = 0
        self.soc = 99
        self.writes = []
        self.failures = []

    def read_holding(self, address, count):
        if self.failures and self.failures[0][0] == "read":
            _, exc = self.failures.pop(0)
            raise exc
        if (address, count) == (trial.MODEL_ADDRESS, trial.MODEL_COUNT):
            return ascii_words(self.model, count)
        if (address, count) == (trial.SERIAL_ADDRESS, trial.SERIAL_COUNT):
            return ascii_words(self.serial, count)
        if (address, count) == (trial.FALLBACK_ADDRESS, 1):
            return [self.fallback]
        if (address, count) == (trial.WORK_MODE_ADDRESS, 1):
            return [self.work_mode]
        if (address, count) == (47509, 4):
            return [self.export_enabled, self.export_limit & 0xFFFF, self.ems_mode, self.target]
        if (address, count) == (trial.EMS_MODE_ADDRESS, 2):
            return [self.ems_mode, self.target]
        if (address, count) == (trial.EMS_MODE_ADDRESS, 1):
            return [self.ems_mode]
        values = {
            35169: [0, 100],
            35171: [0, 500],
            35182: [0, 0],
            35301: [0, 800],
            36025: [0, 0],
            37007: [self.soc],
        }
        if address in values and len(values[address]) == count:
            return list(values[address])
        raise AssertionError((address, count))

    def write_single(self, address, value):
        raise AssertionError("EMS protocol requires FC16 for writes")

    def write_multiple(self, address, values):
        assert address in (trial.EMS_TARGET_ADDRESS, trial.EMS_MODE_ADDRESS)
        assert len(values) == 1
        self.writes.append(("fc16", address, list(values)))
        if address == trial.EMS_TARGET_ADDRESS:
            self.target = values[0]
        else:
            self.ems_mode = values[0]
        self._maybe_fail("write")

    def _maybe_fail(self, kind):
        if self.failures and self.failures[0][0] == kind:
            _, exc = self.failures.pop(0)
            raise exc


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.001)


def run_fast(client, tmp_path, **kwargs):
    clock = FakeClock()
    journal = tmp_path / "journal.json"
    log = tmp_path / "trial.jsonl"
    trial.run_trial(
        client,
        trial.Endpoint("example", 502, 247),
        kwargs.pop("duration", 0.01),
        journal,
        log,
        baseline_seconds=0,
        after_seconds=0,
        heartbeat_seconds=0.005,
        sample_seconds=0.005,
        clock=clock,
        sleeper=clock.sleep,
        **kwargs,
    )
    return journal, log


def make_journal(client, endpoint, path, state="active"):
    _, identity = trial.read_identity(client)
    snapshot = trial.read_snapshot(client, lambda: 0.0)
    data = trial._journal_for(endpoint, identity, snapshot, client.fallback)
    data["state"] = state
    trial.atomic_write_json(path, data, create_only=True)
    return data


def test_inspect_is_read_only_and_hides_serial():
    client = FakeClient()
    result = trial.inspect_endpoint(client, trial.Endpoint("host", 502, 247))
    assert result["trial_ready"]
    assert client.writes == []
    assert client.serial not in json.dumps(result)


def test_inspect_reports_low_soc_not_ready_without_writes():
    client = FakeClient()
    client.soc = 94
    result = trial.inspect_endpoint(client, trial.Endpoint("host", 502, 247))
    assert not result["trial_ready"]
    assert any("95%" in reason for reason in result["reasons"])
    assert client.writes == []


@pytest.mark.parametrize(
    "change,message",
    [
        ({"model": "GW-OTHER"}, "unsupported inverter"),
        ({"work_mode": 2}, "work mode changed"),
        ({"ems_mode": 9}, "EMS mode changed"),
        ({"fallback": 2}, "shutdown watchdog"),
        ({"soc": 89}, "below the 95%"),
    ],
)
def test_preflight_refusals_make_no_writes(tmp_path, change, message):
    client = FakeClient()
    for name, value in change.items():
        setattr(client, name, value)
    with pytest.raises(trial.TrialError, match=message):
        run_fast(client, tmp_path)
    assert client.writes == []
    assert not (tmp_path / "journal.json").exists()


def test_normal_trial_orders_writes_and_restores(tmp_path):
    client = FakeClient()
    journal_path, log_path = run_fast(client, tmp_path)
    assert client.writes[:2] == [
        ("fc16", 47512, [0]),
        ("fc16", 47511, [9]),
    ]
    assert client.writes[-2:] == [
        ("fc16", 47511, [1]),
        ("fc16", 47512, [321]),
    ]
    assert (client.ems_mode, client.target) == (1, 321)
    assert json.loads(journal_path.read_text())["state"] == "restored"
    phases = [json.loads(line)["phase"] for line in log_path.read_text().splitlines()]
    assert {"before", "active", "after"}.issubset(phases)


def test_ambiguous_first_write_is_restored(tmp_path):
    client = FakeClient()
    client.failures = [("write", trial.TrialError("reply lost after apply"))]
    with pytest.raises(trial.TrialError, match="reply lost"):
        run_fast(client, tmp_path)
    assert (client.ems_mode, client.target) == (1, 321)
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "restored"


def test_incomplete_baseline_snapshot_makes_no_writes(tmp_path):
    client = FakeClient()
    client.failures = [("read", trial.TrialError("telemetry unavailable"))]
    with pytest.raises(trial.TrialError, match="telemetry unavailable"):
        run_fast(client, tmp_path)
    assert client.writes == []
    assert not (tmp_path / "journal.json").exists()


def test_readback_mismatch_aborts_and_restores(tmp_path):
    class IgnoresMode(FakeClient):
        def write_multiple(self, address, values):
            if address == trial.EMS_MODE_ADDRESS and values == [9]:
                self.writes.append(("fc16", address, list(values)))
            else:
                super().write_multiple(address, values)

    client = IgnoresMode()
    with pytest.raises(trial.TrialError, match="readback mismatch"):
        run_fast(client, tmp_path)
    assert client.target == 321


def test_interrupt_request_restores(tmp_path):
    client = FakeClient()
    clock = FakeClock()
    guard = trial.SignalGuard()
    calls = 0

    def sleep(seconds):
        nonlocal calls
        clock.sleep(seconds)
        calls += 1
        if calls >= 1 and client.ems_mode == 9:
            guard.requested = 2

    with pytest.raises(trial.TrialError, match="termination requested"):
        trial.run_trial(
            client,
            trial.Endpoint("host", 502, 247),
            2,
            tmp_path / "j.json",
            tmp_path / "l.jsonl",
            baseline_seconds=0,
            after_seconds=0,
            clock=clock,
            sleeper=sleep,
            signal_guard=guard,
        )
    assert (client.ems_mode, client.target) == (1, 321)
    assert guard.restoring


def test_foreign_mode_before_heartbeat_is_not_clobbered(tmp_path):
    client = FakeClient()
    clock = FakeClock()
    changed = False

    def sleep(seconds):
        nonlocal changed
        clock.sleep(seconds)
        if client.ems_mode == 9 and not changed:
            client.ems_mode = 7
            changed = True

    with pytest.raises(trial.TrialError, match="restoration also failed"):
        trial.run_trial(
            client,
            trial.Endpoint("host", 502, 247),
            2,
            tmp_path / "j.json",
            tmp_path / "l.jsonl",
            baseline_seconds=0,
            after_seconds=0,
            clock=clock,
            sleeper=sleep,
        )
    assert [(kind, value) for kind, address, value in client.writes if address == 47511] == [
        ("fc16", [9])
    ]
    assert json.loads((tmp_path / "j.json").read_text())["state"] == "recovery_required"


def test_repeated_signal_during_restore_is_ignored():
    guard = trial.SignalGuard()
    guard.restoring = True
    guard._handler(2, None)
    guard._handler(15, None)
    assert guard.repeated_during_restore
    assert guard.requested is None


def test_failed_restore_retains_recovery_required(tmp_path):
    client = FakeClient()
    endpoint = trial.Endpoint("host", 502, 247)
    journal_path = tmp_path / "j.json"
    data = make_journal(client, endpoint, journal_path)
    client.ems_mode = 9
    client.target = 0
    client.failures = [("read", trial.TrialError("offline"))] * 3
    with pytest.raises(trial.TrialError, match="not confirmed"):
        trial.restore_with_retries(client, data, journal_path, sleeper=lambda _: None)
    saved = json.loads(journal_path.read_text())
    assert saved["state"] == "recovery_required"
    assert saved["restore_attempts"] == 3


@pytest.mark.parametrize("mode,target", [(7, 0), (9, 999)])
def test_recovery_refuses_foreign_control_without_writing(tmp_path, mode, target):
    client = FakeClient()
    endpoint = trial.Endpoint("host", 502, 247)
    journal_path = tmp_path / "j.json"
    data = make_journal(client, endpoint, journal_path)
    client.ems_mode, client.target = mode, target
    with pytest.raises(trial.RestoreRefused):
        trial.restore_with_retries(client, data, journal_path, sleeper=lambda _: None)
    assert client.writes == []
    assert json.loads(journal_path.read_text())["state"] == "recovery_required"


def test_tampered_journal_address_is_rejected(tmp_path):
    client = FakeClient()
    endpoint = trial.Endpoint("host", 502, 247)
    path = tmp_path / "j.json"
    data = make_journal(client, endpoint, path)
    data["addresses"]["ems_mode"] = 1
    path.write_text(json.dumps(data))
    with pytest.raises(trial.TrialError, match="tampered"):
        trial.load_journal(path, endpoint)


def test_endpoint_lock_is_global_across_journal_paths():
    endpoint = trial.Endpoint("same-host", 502, 247)
    with trial.endpoint_lock(endpoint):
        with pytest.raises(trial.LockBusy):
            with trial.endpoint_lock(endpoint):
                pass


def test_existing_journal_is_never_overwritten(tmp_path):
    client = FakeClient()
    path = tmp_path / "journal.json"
    path.write_text("keep me")
    with pytest.raises(trial.TrialError, match="already exists"):
        run_fast(client, tmp_path)
    assert path.read_text() == "keep me"
    assert client.writes == []


def test_existing_log_is_not_appended_or_contacted(tmp_path):
    client = FakeClient()
    path = tmp_path / "trial.jsonl"
    path.write_text("older trial\n")
    with pytest.raises(trial.TrialError, match="log already exists"):
        run_fast(client, tmp_path)
    assert path.read_text() == "older trial\n"
    assert client.writes == []


def serve_once(builder, keep_open=0.0):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def worker():
        try:
            conn, _ = listener.accept()
            with conn:
                header = conn.recv(7)
                txid, proto, length, unit = struct.unpack(">HHHB", header)
                pdu = b""
                while len(pdu) < length - 1:
                    pdu += conn.recv(length - 1 - len(pdu))
                conn.sendall(builder(txid, proto, unit, pdu))
                if keep_open:
                    time.sleep(keep_open)
        finally:
            listener.close()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return port, thread


def frame(txid, unit, pdu, *, protocol=0, length=None):
    return struct.pack(">HHHB", txid, protocol, length or len(pdu) + 1, unit) + pdu


def test_modbus_valid_frame_does_not_wait_for_peer_close():
    def response(txid, _proto, unit, _request):
        return frame(txid, unit, b"\x03\x02\x12\x34")

    port, thread = serve_once(response, keep_open=0.3)
    client = trial.ModbusTcpClient(trial.Endpoint("127.0.0.1", port, 247), timeout=0.1)
    started = time.monotonic()
    assert client.read_holding(100, 1) == [0x1234]
    assert time.monotonic() - started < 0.2
    thread.join(1)


@pytest.mark.parametrize(
    "builder,method,match",
    [
        (lambda tx, _p, unit, _r: frame(tx + 1, unit, b"\x03\x02\x00\x01"), "read", "transaction"),
        (lambda tx, _p, unit, _r: frame(tx, unit, b"\x06\x00\x01\x00\x02"), "write", "echo"),
        (lambda tx, _p, unit, _r: frame(tx, unit, b"\x83\x02"), "read", "exception"),
    ],
)
def test_modbus_rejects_hostile_mbap_and_write_echo(builder, method, match):
    port, thread = serve_once(builder)
    client = trial.ModbusTcpClient(trial.Endpoint("127.0.0.1", port, 247), timeout=0.5)
    with pytest.raises(trial.ProtocolError, match=match):
        if method == "read":
            client.read_holding(1, 1)
        else:
            client.write_single(1, 1)
    thread.join(1)


def test_modbus_rejects_fc16_echo_mismatch():
    def response(txid, _proto, unit, _request):
        return frame(txid, unit, struct.pack(">BHH", 16, 47512, 2))

    port, thread = serve_once(response)
    client = trial.ModbusTcpClient(trial.Endpoint("127.0.0.1", port, 247), timeout=0.5)
    with pytest.raises(trial.ProtocolError, match="FC16 echo mismatch"):
        client.write_multiple(47512, [0])
    thread.join(1)


def test_cli_run_requires_all_acknowledgements(tmp_path):
    with pytest.raises(SystemExit):
        trial.main(
            [
                "--host",
                "127.0.0.1",
                "--journal",
                str(tmp_path / "j"),
                "run",
                "--log",
                str(tmp_path / "l"),
                "--execute",
            ]
        )
    with pytest.raises(SystemExit):
        trial.build_parser().parse_args(["--host", "x", "run", "--log", "x", "--duration", "30.5"])


def test_cli_defaults_to_read_only_inspection(monkeypatch, capsys):
    client = FakeClient()
    monkeypatch.setattr(trial, "ModbusTcpClient", lambda *args: client)
    assert trial.main(["--host", "192.0.2.91"]) == 0
    assert client.writes == []
    result = json.loads(capsys.readouterr().out)
    assert result["tool_version"] == "0.1.0-beta.1"
    assert result["physical_success_claimed"] is False


def test_recovery_refuses_a_replaced_device(tmp_path):
    client = FakeClient()
    endpoint = trial.Endpoint("192.0.2.91", 502, 247)
    path = tmp_path / "journal.json"
    make_journal(client, endpoint, path)
    client.ems_mode, client.target = 9, 0
    client.serial = "REPLACED-DEVICE"
    with pytest.raises(trial.TrialError, match="identity"):
        trial.recover(client, endpoint, path)
    assert client.writes == []
    assert json.loads(path.read_text())["state"] == "active"


def test_automatic_restore_refuses_a_replaced_device(tmp_path):
    client = FakeClient()
    endpoint = trial.Endpoint("192.0.2.91", 502, 247)
    path = tmp_path / "journal.json"
    journal = make_journal(client, endpoint, path)
    client.ems_mode, client.target = 9, 0
    client.serial = "REPLACED-DEVICE"
    with pytest.raises(trial.RestoreRefused, match="identity"):
        trial.restore_with_retries(client, journal, path)
    assert client.writes == []
    assert json.loads(path.read_text())["state"] == "recovery_required"


def test_active_low_soc_stops_and_restores(tmp_path):
    client = FakeClient()
    clock = FakeClock()

    def sleep(seconds):
        clock.sleep(seconds)
        if client.ems_mode == 9:
            client.soc = 94

    with pytest.raises(trial.TrialError, match="below the 95%"):
        trial.run_trial(
            client, trial.Endpoint("host", 502, 247), 6,
            tmp_path / "journal.json", tmp_path / "log.jsonl",
            baseline_seconds=0, after_seconds=0, clock=clock, sleeper=sleep,
        )
    assert (client.ems_mode, client.target) == (1, 321)
    assert json.loads((tmp_path / "journal.json").read_text())["state"] == "restored"


def test_changed_fallback_stops_without_rewriting_it(tmp_path):
    client = FakeClient()
    clock = FakeClock()

    def sleep(seconds):
        clock.sleep(seconds)
        if client.ems_mode == 9:
            client.fallback = 2

    with pytest.raises(trial.TrialError, match="fallback setting changed"):
        trial.run_trial(
            client, trial.Endpoint("host", 502, 247), 2,
            tmp_path / "journal.json", tmp_path / "log.jsonl",
            baseline_seconds=0, after_seconds=0, clock=clock, sleeper=sleep,
        )
    assert client.fallback == 2
    assert (client.ems_mode, client.target) == (1, 321)
    assert all(address in (47511, 47512) for _, address, _ in client.writes)


def test_slow_sample_is_not_treated_as_fresh():
    client = FakeClient()
    times = iter([0.0, 2.1])
    with pytest.raises(trial.TrialError, match="freshness budget"):
        trial.read_snapshot(client, lambda: next(times))
    assert client.writes == []


@contextlib.contextmanager
def simulated_inverter():
    device = FakeClient()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(0.1)
    endpoint = trial.Endpoint("127.0.0.1", listener.getsockname()[1], 247)
    stop = threading.Event()
    errors = []
    requests = []

    def worker():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with conn:
                    conn.settimeout(1)
                    header = trial.ModbusTcpClient._recv_exact(conn, 7, time.monotonic() + 1)
                    txid, _, length, unit = struct.unpack(">HHHB", header)
                    pdu = trial.ModbusTcpClient._recv_exact(conn, length - 1, time.monotonic() + 1)
                    function, address, count = struct.unpack(">BHH", pdu[:5])
                    requests.append((function, address, count))
                    if function == 3:
                        words = device.read_holding(address, count)
                        response = bytes([3, len(words) * 2]) + struct.pack(f">{len(words)}H", *words)
                    else:
                        assert function == 0x10
                        assert address in (47511, 47512) and count == 1
                        assert pdu[5] == 2 and len(pdu) == 8
                        words = list(struct.unpack(">H", pdu[6:]))
                        device.write_multiple(address, words)
                        response = pdu[:5]
                    conn.sendall(frame(txid, unit, response))
            except BaseException as exc:
                errors.append(exc)
                break

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        yield endpoint, device, requests
    finally:
        stop.set()
        thread.join(2)
        listener.close()
        assert not thread.is_alive()
        assert not errors, errors


def test_full_trial_over_real_tcp_uses_only_documented_write_function(tmp_path):
    with simulated_inverter() as (endpoint, device, requests):
        clock = FakeClock()
        client = trial.ModbusTcpClient(endpoint, timeout=0.5)
        trial.run_trial(
            client, endpoint, 0.01, tmp_path / "journal.json", tmp_path / "log.jsonl",
            baseline_seconds=0, after_seconds=0, heartbeat_seconds=0.005,
            sample_seconds=0.005, clock=clock, sleeper=clock.sleep,
        )
        assert (device.ems_mode, device.target) == (1, 321)
        writes = [(function, address) for function, address, _ in requests if function != 3]
        assert writes[:2] == [(0x10, 47512), (0x10, 47511)]
        assert writes[-2:] == [(0x10, 47511), (0x10, 47512)]
        assert all(function == 0x10 and address in (47511, 47512) for function, address in writes)
        assert json.loads((tmp_path / "journal.json").read_text())["state"] == "restored"
