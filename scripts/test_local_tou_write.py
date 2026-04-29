"""Phase 0 test: can we write TOU tariff to the gateway via local V1R write_config?

Run on the HA host inside the core-ssh add-on (Python and deps already installed
from last session). Imports the integration's transport module from a copy at
/tmp/pw_local — does NOT touch the running integration.

Steps:
    1. Read paired-site creds from /config/.storage/core.config_entries.
    2. read_config() and locate tou_settings.tariff_content_v2 in the live config.
    3. If absent: report "no tariff present, sync via cloud first" and exit.
    4. Save the current tariff. Write it back unchanged via write_config.
       Confirm no fault, hash advances, the same tariff comes back on re-read.
    5. Mutate a single rate (peak buy +$0.01). Write. Re-read. Confirm mutation.
    6. Restore the original tariff (write again). Confirm restore.
    7. Print PASS/FAIL summary.

If steps 4–6 all pass: gateway accepts arbitrary tariff JSON via local writes,
and Phase 6 (TOU local sync) can proceed. If any step fails: keep cloud TOU
sync, report the gateway response.

Usage:
    cp -r /config/custom_components/power_sync/powerwall_local /tmp/pw_local  # if not done
    python3 /tmp/test_local_tou_write.py
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

STORAGE = Path(os.environ.get("HA_STORAGE", "/config/.storage/core.config_entries"))
TARIFF_PATH = "tou_settings.tariff_content_v2"
RATE_BUMP = 0.01


def load_creds() -> dict:
    entries = json.loads(STORAGE.read_text())["data"]["entries"]
    ps = next((e for e in entries if e["domain"] == "power_sync"), None)
    if ps is None:
        raise SystemExit(f"No power_sync entry in {STORAGE}")
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
        raise SystemExit(f"Missing keys: {missing}")
    return {
        "host": d["powerwall_local_ip"],
        "din": d["powerwall_local_din"],
        "password": d["powerwall_local_customer_password"],
        "key_pem": d["powerwall_local_private_key_pem"].encode(),
    }


def get_dotted(d: dict, path: str):
    node = d
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def first_buy_rate_path(tariff: dict) -> tuple[list, float] | None:
    """Find a (path, value) inside the tariff to mutate. Returns (key-path, original)."""
    energy_charges = tariff.get("energy_charges") or {}
    for season_name, season in energy_charges.items():
        rates = (season or {}).get("rates") or {}
        if rates:
            period = next(iter(rates))
            return (["energy_charges", season_name, "rates", period], rates[period])
    return None


def set_dotted(d: dict, path: list, value) -> None:
    node = d
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value


async def run() -> int:
    sys.path.insert(0, "/tmp")
    try:
        from pw_local.transport import TEDAPIv1rTransport
    except ImportError as err:
        raise SystemExit(
            f"Cannot import pw_local.transport ({err}); copy with:\n"
            "  cp -r /config/custom_components/power_sync/powerwall_local /tmp/pw_local"
        )

    c = load_creds()
    print(f"[*] Gateway: {c['host']}  DIN: {c['din']}")

    t = TEDAPIv1rTransport(c["host"], c["key_pem"], c["password"])
    if not await t.login():
        raise SystemExit("login failed")
    din = await t.fetch_din() or c["din"]

    print("\n[1] Reading current config.json")
    cfg = await t.read_config(din)
    if not cfg:
        raise SystemExit("read_config returned nothing")
    tariff = get_dotted(cfg, TARIFF_PATH)
    if not tariff or not isinstance(tariff, dict):
        # Fall back: PW3 may store it directly at "tariff_content_v2"
        tariff = cfg.get("tariff_content_v2")
        if isinstance(tariff, dict):
            actual_path = "tariff_content_v2"
        else:
            print(
                "[FAIL] No tariff found at "
                f"{TARIFF_PATH} or tariff_content_v2.\n"
                "       Sync a tariff via cloud first, then re-run."
            )
            print("       Top-level config keys:", sorted(cfg.keys()))
            return 1
    else:
        actual_path = TARIFF_PATH
    print(f"      found tariff at: {actual_path}")
    print(f"      tariff name:     {tariff.get('name')!r}")
    print(f"      tariff version:  {tariff.get('version')!r}")

    target = first_buy_rate_path(tariff)
    if not target:
        print("[FAIL] tariff has no buy-rate periods to mutate")
        return 1
    rate_path, original_rate = target
    print(f"      will mutate:     {'/'.join(rate_path)} = {original_rate}")

    original_tariff = copy.deepcopy(tariff)

    print(f"\n[2] Writing tariff back UNCHANGED via {actual_path}")
    ok = await t.write_config(din, {actual_path: tariff})
    print(f"      write_config -> {ok}")
    if not ok:
        print("[FAIL] gateway rejected unchanged write — local TOU sync NOT viable")
        return 1

    print("\n[3] Re-reading to confirm tariff persisted")
    cfg2 = await t.read_config(din)
    tariff2 = get_dotted(cfg2, actual_path) if "." in actual_path else cfg2.get(actual_path)
    if tariff2 != original_tariff:
        print("[FAIL] tariff differs after unchanged write")
        return 1
    print("      tariff matches (unchanged write OK)")

    print(f"\n[4] Mutating {'/'.join(rate_path)}: {original_rate} -> {original_rate + RATE_BUMP}")
    mutated = copy.deepcopy(original_tariff)
    set_dotted(mutated, rate_path, original_rate + RATE_BUMP)
    ok = await t.write_config(din, {actual_path: mutated})
    print(f"      write_config -> {ok}")
    if not ok:
        print("[FAIL] gateway rejected mutated write")
        return 1

    await asyncio.sleep(3)
    print("\n[5] Re-reading to confirm mutation took")
    cfg3 = await t.read_config(din)
    tariff3 = get_dotted(cfg3, actual_path) if "." in actual_path else cfg3.get(actual_path)
    node = tariff3
    for k in rate_path:
        node = node[k] if isinstance(node, dict) else None
        if node is None:
            break
    print(f"      readback value:  {node}")
    if node is None or abs(float(node) - (original_rate + RATE_BUMP)) > 1e-6:
        print("[FAIL] mutation did NOT persist on gateway")
        # Try restore even on failure
    else:
        print("      mutation persisted ✓")

    print(f"\n[6] Restoring original tariff")
    ok = await t.write_config(din, {actual_path: original_tariff})
    print(f"      write_config -> {ok}")

    await asyncio.sleep(2)
    cfg4 = await t.read_config(din)
    tariff4 = get_dotted(cfg4, actual_path) if "." in actual_path else cfg4.get(actual_path)
    if tariff4 == original_tariff:
        print("      restore OK ✓")
    else:
        print("      ⚠ restore did not exactly match — review manually")

    if node is not None and abs(float(node) - (original_rate + RATE_BUMP)) <= 1e-6:
        print("\n[PASS] Local TOU write is viable. Phase 6 can proceed.")
        return 0
    print("\n[FAIL] Mutation did not stick. Keep cloud TOU sync.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
