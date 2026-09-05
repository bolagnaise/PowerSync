<!-- release: v2.12.1237 -->

## What's Changed

**Keep stale Tesla BLE power out of live EV status**
Tesla BLE charge power now uses the same 90-second freshness rule as EV attribution and Home Load normalization. When a charger is still connected or charging but its measured watts are stale, PowerSync keeps that status evidence while showing power as unavailable instead of presenting old watts as a current observed draw. This prevents a blank fail-closed Home Load from appearing beside a contradictory live EV-power value.

**Prefer the newest compatible Tesla BLE power reading**
When both legacy and current Tesla BLE power or measured-current entities are available, PowerSync now uses the newest finite reading instead of taking the first compatible alias. This avoids an older compatibility entity masking a fresher measurement.

Update available via HACS
