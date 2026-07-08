<!-- release: v2.12.789 -->

## What's Changed

**Tesla BLE + Teslemetry duplicate EV recovery guard**
PowerSync no longer lets a Tesla BLE fallback entry with unknown SOC start price-level recovery charging when a real Tesla/Fleet/Teslemetry vehicle is already discovered for the same setup. Mixed Fleet/Teslemetry + BLE installs should stop seeing a second "Tesla BLE" vehicle trigger recovery charging just because the BLE side has no SOC sensor, while BLE-only and generic unknown-SOC setups keep the existing fallback behavior.

**EV scheduling uses Home Assistant local time**
EV plan weekday selection, forecast staleness checks, and historical load bucketing now use Home Assistant local time instead of UTC/host time. This prevents overnight EV windows and learned load profiles from being shifted into the wrong day or hour on non-UTC systems.

**Optimizer and restore handoff fixes**
The optimizer now preserves LP charge slots through export-spreading handoffs, and Modbus restore paths re-check superseded restore work before every brand branch. This reduces stale restore/write churn when a newer command has already taken ownership.

**Powerwall and coordinator cleanup**
PowerSync now reconnects an orphaned off-grid local Powerwall coordinator on startup and shuts down brand/local coordinators more completely on unload, reducing stale background work after reloads.

Update available via HACS
