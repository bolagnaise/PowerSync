"""Standalone V1R local-only round-trip test.

Run on the Home Assistant host (where the power_sync integration is installed).
It reads credentials from /config/.storage/core.config_entries, uses the
integration's TEDAPIv1rTransport to login, fetch DIN, and read config from the
Powerwall 3 gateway over the LAN. No Tesla cloud calls.

Usage (HA OS / Supervised — runs inside the homeassistant container):
    docker exec homeassistant python /config/test_v1r_local.py

Usage (HA Core venv):
    /usr/src/homeassistant/.venv/bin/python /config/test_v1r_local.py

To prove local-only, in a separate terminal on the HA host run:
    sudo tcpdump -i any -n -q "not host <GATEWAY_IP> and not net 192.168.0.0/16 and not net 100.64.0.0/10 and not net 10.0.0.0/8 and port not 53"
and confirm zero packets while the script is running.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

STORAGE = Path(os.environ.get("HA_STORAGE", "/config/.storage/core.config_entries"))
INTEGRATION_ROOT = Path(
    os.environ.get(
        "POWER_SYNC_ROOT", "/config/custom_components/power_sync"
    )
)


def load_creds() -> dict:
    entries = json.loads(STORAGE.read_text())["data"]["entries"]
    ps = next((e for e in entries if e["domain"] == "power_sync"), None)
    if ps is None:
        raise SystemExit(f"No power_sync config entry in {STORAGE}")
    d = ps["data"]
    missing = [
        k for k in (
            "powerwall_local_ip",
            "powerwall_local_din",
            "powerwall_local_customer_password",
            "powerwall_local_private_key_pem",
        ) if not d.get(k)
    ]
    if missing:
        raise SystemExit(f"Missing keys in entry: {missing}")
    return {
        "host": d["powerwall_local_ip"],
        "din": d["powerwall_local_din"],
        "password": d["powerwall_local_customer_password"],
        "key_pem": d["powerwall_local_private_key_pem"].encode(),
    }


async def run() -> int:
    sys.path.insert(0, str(INTEGRATION_ROOT.parent))
    from power_sync.powerwall_local.transport import TEDAPIv1rTransport  # type: ignore

    c = load_creds()
    print(f"[*] Gateway:   {c['host']}")
    print(f"[*] DIN (cfg): {c['din']}")
    print(f"[*] Pwd len:   {len(c['password'])}")
    print(f"[*] Key bytes: {len(c['key_pem'])}")

    t = TEDAPIv1rTransport(c["host"], c["key_pem"], c["password"])

    print("\n[1] login() — POST https://<gw>/api/login/Basic")
    ok = await t.login()
    print(f"    -> {'PASS' if ok else 'FAIL'}")
    if not ok:
        return 1

    print("\n[2] fetch_din() — GET https://<gw>/tedapi/din")
    din = await t.fetch_din()
    print(f"    -> {din!r}")
    if not din:
        return 1
    if din.strip() != c["din"].strip():
        print(f"    !! gateway DIN ({din}) != stored DIN ({c['din']})")

    print("\n[3] read_config() — RSA-signed RoutableMessage POST /tedapi/v1r")
    cfg = await t.read_config(din)
    if not cfg:
        print("    -> FAIL (no config returned — likely UNKNOWN_KEY_ID)")
        return 1
    top = sorted(cfg.keys())[:8]
    print(f"    -> PASS ({len(cfg)} top-level keys, sample: {top})")

    print("\nResult: V1R signed round-trip succeeded against the LAN gateway.")
    print("        Run with tcpdump filtered to non-LAN to prove no cloud call.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
