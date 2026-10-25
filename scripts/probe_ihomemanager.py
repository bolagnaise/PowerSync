#!/usr/bin/env python3
"""Read native iHM compatibility data. Never issues writes or enables dispatch.

Requires Python 3.12 and pymodbus (available in Home Assistant's environment).
Run from this checkout: python3.12 scripts/probe_ihomemanager.py --host LOCAL_IP
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys


def load_driver():
    path = Path(__file__).resolve().parents[1] / "custom_components/power_sync/inverters/sungrow_ihomemanager.py"
    spec = importlib.util.spec_from_file_location("ihomemanager_probe_driver", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.SungrowIHomeManagerController


async def probe(host: str, port: int) -> dict:
    controller = load_driver()(host, port)  # No control permission callback.
    snapshot = await controller.read_snapshot()
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "unit": controller.UNIT,
        "port": port,
        "protocol_version_hex": f"0x{snapshot.protocol_version:08X}",
        "documented_dispatch_protocol": snapshot.supports_dispatch,
        "control_enabled": False,
        "native": asdict(snapshot),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Local iHomeManager IP or hostname")
    parser.add_argument("--port", type=int, choices=(503, 504), default=503)
    args = parser.parse_args()
    try:
        result = asyncio.run(probe(args.host, args.port))
    except Exception as err:
        print(json.dumps({"error": str(err), "control_enabled": False}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
