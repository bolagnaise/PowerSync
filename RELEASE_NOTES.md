<!-- release: v2.12.1229 -->

## What's Changed

**Tesla BLE charging-block detection now follows fresh per-vehicle power**
When two Tesla BLE bridges are configured, a disconnected vehicle can no longer hide another vehicle that has a fresh positive charger-power reading but an unavailable charging-state entity. Existing `charging_starts` automations now select the active vehicle and retain its exact BLE target for the configured stop action. Stale power and bridge availability alone still do not count as charging.

**Unknown EV telemetry is shown as unknown**
The EV dashboard now displays `--` for unavailable power, current, and state of charge instead of presenting missing data as `0.00 kW`, `0 A`, or `0%`.

Update available via HACS
