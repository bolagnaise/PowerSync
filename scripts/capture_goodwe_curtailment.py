#!/usr/bin/env python3
"""Read-only GoodWe ESA Modbus TCP capture; Python 3.9+, no dependencies.

See docs/goodwe-curtailment-capture.md. This file deliberately implements only
function 03 (read holding registers), with a fixed list of register blocks.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import socket
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# Address, count, fields (name, word offset, number of words, signed).
# GoodWe Third-Party EMS Protocol MODBUS V1.1.1, pp. 14-21; meter status and
# operation mode additionally follow marcelblijleven/goodwe's ET mapping.
# These are literal wire addresses, with NO subtraction of 1 or 40001.
BLOCKS = {
    "legacy_controls": (47509, 4, (
        ("export_limit_enabled", 0, 1, False),
        ("export_limit_w", 1, 1, True),
        ("ems_mode", 2, 1, False),
        ("ems_power_w", 3, 1, False),
    )),
    "extended_controls": (42000, 6, (
        ("extended_ems_mode", 0, 1, False),
        ("extended_ems_power_w", 1, 2, False),
        ("extended_export_limit_enabled", 3, 1, False),
        ("extended_export_limit_w", 4, 2, True),
    )),
    "fallback": (47505, 1, (("ems_fallback_mode", 0, 1, False),)),
    "work_mode": (35187, 2, (
        ("work_mode", 0, 1, False), ("operation_mode", 1, 1, False),
    )),
    "pv": (35301, 2, (("pv_w", 0, 2, False),)),
    "inverter_grid": (35137, 4, (
        ("inverter_w", 0, 2, True), ("ac_active_w", 2, 2, True),
    )),
    "load": (35169, 4, (
        ("backup_load_w", 0, 2, True), ("on_grid_load_w", 2, 2, True),
    )),
    "battery": (35182, 3, (
        ("battery_w", 0, 2, True), ("battery_mode", 2, 1, False),
    )),
    "soc": (37007, 1, (("battery_soc_pct", 0, 1, False),)),
    "meter_status": (36003, 2, (
        ("meter_test_status", 0, 1, False),
        ("meter_comm_status", 1, 1, False),
    )),
    "meter_power": (36025, 2, (("meter_active_w", 0, 2, True),)),
}
FIELDS = [field[0] for _, _, fields in BLOCKS.values() for field in fields]


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class ReadError(Exception):
    """Unusable response; never interpret it as a zero reading."""


class DeviceError(ReadError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"Modbus exception {code}")


def receive_exact(sock, count):
    chunks = bytearray()
    while len(chunks) < count:
        chunk = sock.recv(count - len(chunks))
        if not chunk:
            raise ReadError("Connection closed before the complete response")
        chunks.extend(chunk)
    return bytes(chunks)


def read_block(sock, unit, transaction, name, audit):
    """The only network request builder: allowlisted function-03 reads."""
    address, count, fields = BLOCKS[name]
    request = struct.pack(">HHHBBHH", transaction, 0, 6, unit, 3, address, count)
    event = {"started_utc": utc_now(), "block": name, "address": address,
             "count": count, "request_hex": request.hex()}
    received = bytearray()
    try:
        sock.sendall(request)
        header = receive_exact(sock, 7)
        received.extend(header)
        reply_id, protocol, length, reply_unit = struct.unpack(">HHHB", header)
        if reply_id != transaction or protocol != 0 or reply_unit != unit:
            raise ReadError("Response transaction/protocol/unit does not match")
        if not 2 <= length <= 254:
            raise ReadError("Invalid Modbus response length")
        body = receive_exact(sock, length - 1)
        received.extend(body)
        if body[0] == 0x83:
            if len(body) != 2:
                raise ReadError("Malformed Modbus exception")
            raise DeviceError(body[1])
        if body[0] != 3 or len(body) != 2 + 2 * count or body[1] != 2 * count:
            raise ReadError("Wrong function or register byte count")
        words = list(struct.unpack(">" + "H" * count, body[2:]))
        event["words"] = words
        return {
            field: int.from_bytes(body[2 + 2 * offset:2 + 2 * (offset + size)],
                                  "big", signed=signed)
            for field, offset, size, signed in fields
        }
    except (OSError, ReadError) as exc:
        event["error"] = str(exc)
        raise
    finally:
        event["finished_utc"] = utc_now()
        event["response_hex"] = received.hex()
        audit.write(json.dumps(event) + "\n")
        audit.flush()


def collect_sample(host, port, unit, timeout, disabled, audit):
    """Fresh values only; one short-lived TCP connection for each sample."""
    row = {"started_utc": utc_now()}
    errors = {}
    transport_ok = True
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            for index, name in enumerate(BLOCKS, 1):
                if name in disabled:
                    errors[name] = "Unsupported earlier in this capture (exception 2)"
                    continue
                try:
                    row.update(read_block(sock, unit, index, name, audit))
                except DeviceError as exc:
                    errors[name] = str(exc)
                    if exc.code == 2:
                        disabled.add(name)
                    else:
                        # Busy/other device errors: leave the device alone for
                        # the rest of this sample, rather than hammering it.
                        transport_ok = False
                        break
                time.sleep(0.25)
    except (OSError, ReadError) as exc:
        errors["connection"] = str(exc)
        transport_ok = False
    row["finished_utc"] = utc_now()
    row["errors"] = json.dumps(errors, sort_keys=True)
    # Blank CSV cells mean missing, never zero or a carried-forward value.
    return row, transport_ok and any(field in row for field in FIELDS)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="Inverter's existing LAN IP; no discovery/scanning")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=247,
                        help="Existing Modbus unit ID (default: 247)")
    parser.add_argument("--interval", type=float, default=30,
                        help="Seconds between samples, minimum 10 (default: 30)")
    parser.add_argument("--minutes", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=3)
    parser.add_argument("--check", action="store_true", help="Capture just one sample")
    parser.add_argument("--output", type=Path,
                        help="New directory; existing directories are never overwritten")
    parser.add_argument("--note", default="",
                        help="Optional installed versions/setup note; omit account details")
    args = parser.parse_args(argv)
    for name in ("interval", "minutes", "timeout"):
        if not math.isfinite(getattr(args, name)):
            parser.error(f"--{name} must be finite")
    if args.interval < 10 or not 0 < args.minutes <= 1440 or not 0 < args.timeout <= 10:
        parser.error("Require interval >= 10s, 0 < minutes <= 1440, 0 < timeout <= 10s")
    if not 1 <= args.unit <= 247 or not 1 <= args.port <= 65535:
        parser.error("Require unit 1..247 and port 1..65535")
    return args


def main(argv=None):
    args = parse_args(argv)
    directory = args.output or Path(datetime.now(timezone.utc).strftime(
        "goodwe-capture-%Y%m%dT%H%M%S-%fZ"))
    directory.mkdir(parents=True, exist_ok=False)
    metadata = {
        "started_utc": utc_now(), "python": sys.version.split()[0],
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "unit": args.unit, "port": args.port, "interval_s": args.interval,
        "duration_minutes": args.minutes, "check_only": args.check,
        "note": args.note, "protocol": "GoodWe Third-Party EMS MODBUS V1.1.1",
        "blocks": {name: {"address": b[0], "count": b[1]}
                   for name, b in BLOCKS.items()},
        "limitations": "Sequential device reads, not an atomic snapshot or command trace. "
                        "Meter coverage/sign must be checked independently. No serials read.",
    }
    meta_path = directory / "metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"Read-only capture: {directory.resolve()} (Ctrl+C to stop)", flush=True)
    deadline = time.monotonic() + args.minutes * 60
    disabled = set()
    failures = 0
    samples = 0
    status = 0
    try:
        with (directory / "capture.csv").open("x", newline="", encoding="utf-8") as csv_file, \
             (directory / "readbacks.jsonl").open("x", encoding="utf-8") as audit:
            writer = csv.DictWriter(csv_file, ["started_utc", "finished_utc", *FIELDS, "errors"])
            writer.writeheader()
            while time.monotonic() < deadline:
                started = time.monotonic()
                row, ok = collect_sample(args.host, args.port, args.unit,
                                         args.timeout, disabled, audit)
                writer.writerow(row)
                csv_file.flush()
                samples += 1
                failures = 0 if ok else failures + 1
                print(f"{row['finished_utc']} sample {samples}: "
                      f"{'read complete' if ok else 'read failed'}; errors={row['errors']}",
                      flush=True)
                if failures >= 3 or (args.check and not ok):
                    status = 1
                    break
                if args.check:
                    break
                # No catch-up bursts after a slow sample.
                pause = min(max(1, args.interval - (time.monotonic() - started)),
                            max(0, deadline - time.monotonic()))
                time.sleep(pause)
    except KeyboardInterrupt:
        print("Stopped; completed readings have been saved.")
    finally:
        metadata.update(finished_utc=utc_now(), samples=samples,
                        unsupported_blocks=sorted(disabled), exit_status=status)
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return status


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f"Capture stopped: {exc}", file=sys.stderr)
        sys.exit(1)
