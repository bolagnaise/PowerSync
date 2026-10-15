"""Protocol and failure-path checks for the standalone, read-only collector."""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import struct
import threading
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "goodwe_capture", Path(__file__).resolve().parents[1]
    / "scripts" / "capture_goodwe_curtailment.py"
)
capture = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(capture)


def response(words, transaction=1, unit=247, function=3):
    data = struct.pack(">" + "H" * len(words), *words)
    return struct.pack(">HHHBBB", transaction, 0, len(data) + 3,
                       unit, function, len(data)) + data


class FakeSocket:
    def __init__(self, reply):
        self.reply = bytearray(reply)
        self.sent = []

    def sendall(self, data):
        self.sent.append(data)

    def recv(self, count):
        # Deliberate TCP fragmentation, including fragmented MBAP header.
        result = self.reply[:min(count, 2)]
        del self.reply[:len(result)]
        return result


def test_reads_signed_full_width_and_literal_address():
    sock = FakeSocket(response([0xFFFF, 0xEC78, 0, 10000]))
    audit = io.StringIO()
    result = capture.read_block(sock, 247, 1, "inverter_grid", audit)
    assert result == {"inverter_w": -5000, "ac_active_w": 10000}
    assert struct.unpack(">HHHBBHH", sock.sent[0]) == (1, 0, 6, 247, 3, 35137, 4)
    assert json.loads(audit.getvalue())["words"] == [65535, 60536, 0, 10000]


@pytest.mark.parametrize("reply", [
    response([1, 0, 1, 0], transaction=2),
    response([1, 0, 1, 0], unit=1),
    response([1, 0, 1, 0], function=6),
    response([1, 0]),
    response([1, 0, 1, 0])[:-1],
    struct.pack(">HHHB", 1, 0, 65535, 247),
])
def test_rejects_mismatched_malformed_and_truncated_responses(reply):
    audit = io.StringIO()
    with pytest.raises(capture.ReadError):
        capture.read_block(FakeSocket(reply), 247, 1, "legacy_controls", audit)
    assert "error" in json.loads(audit.getvalue())


def test_legacy_signed_limit_and_extended_32bit_power():
    legacy = capture.read_block(FakeSocket(response([1, 65535, 10, 0])),
                                247, 1, "legacy_controls", io.StringIO())
    extended = capture.read_block(FakeSocket(response([10, 1, 4464, 1, 65535, 64536])),
                                  247, 1, "extended_controls", io.StringIO())
    assert legacy["export_limit_w"] == -1
    assert extended["extended_ems_power_w"] == 70000
    assert extended["extended_export_limit_w"] == -1000


def test_real_tcp_capture_emits_only_allowlisted_reads_and_keeps_missing_blank(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    port = listener.getsockname()[1]
    observed = []
    server_errors = []

    def serve():
        try:
            with listener:
                conn, _ = listener.accept()
                with conn:
                    conn.settimeout(5)
                    for _ in capture.BLOCKS:
                        req = capture.receive_exact(conn, 12)
                        tid, proto, length, unit, function, address, count = struct.unpack(
                            ">HHHBBHH", req)
                        observed.append((function, address, count))
                        if address == 42000:
                            conn.sendall(struct.pack(">HHHBBB", tid, proto, 3, unit, 0x83, 2))
                        else:
                            conn.sendall(response([0] * count, transaction=tid, unit=unit))
        except Exception as exc:
            server_errors.append(exc)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    disabled = set()
    row, ok = capture.collect_sample("127.0.0.1", port, 247, 1,
                                     disabled, io.StringIO())
    thread.join(6)
    assert not thread.is_alive()
    assert not server_errors
    assert ok
    assert observed == [(3, address, count) for address, count, _ in capture.BLOCKS.values()]
    assert disabled == {"extended_controls"}
    assert "extended_ems_mode" not in row
    assert row["export_limit_enabled"] == 0
    assert "extended_controls" in json.loads(row["errors"])


def test_transport_failure_does_not_reuse_prior_values(monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(capture.socket, "create_connection", fail)
    row, ok = capture.collect_sample("unused", 502, 247, 1, set(), io.StringIO())
    assert not ok
    assert not set(capture.FIELDS).intersection(row)
    assert "timed out" in row["errors"]


def test_three_failed_samples_stop_and_preserve_output(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        return {"started_utc": "start", "finished_utc": "end", "errors": "{}"}, False

    monkeypatch.setattr(capture, "collect_sample", fail)
    monkeypatch.setattr(capture.time, "sleep", lambda _: None)
    directory = tmp_path / "capture"
    assert capture.main(["unused", "--output", str(directory)]) == 1
    metadata = json.loads((directory / "metadata.json").read_text())
    assert metadata["samples"] == 3
    assert metadata["exit_status"] == 1
    with pytest.raises(FileExistsError):
        capture.main(["unused", "--output", str(directory)])


@pytest.mark.parametrize("args", [
    ["--interval", "1"], ["--interval", "nan"], ["--minutes", "inf"],
    ["--minutes", "0"], ["--timeout", "0"], ["--unit", "0"], ["--port", "65536"],
])
def test_invalid_or_aggressive_polling_is_rejected(args):
    with pytest.raises(SystemExit):
        capture.parse_args(["unused", *args])
