<!-- release: v2.12.788 -->

## What's Changed

**Fronius GEN24/BYD storage commands recover from stale entity maps**
PowerSync now refreshes and validates Fronius GEN24 storage-control entities immediately before force-charge, force-discharge, idle, restore, and reserve writes. If the upstream Fronius Modbus integration exposes read sensors before the writable `battery_api_mode` or `storage_control_mode` entities are ready, PowerSync will retry discovery and fail cleanly instead of raising raw `KeyError` traces or sending commands to unavailable entities.

**Grid-charge SOC caps apply to grid-filled battery energy**
The Smart Optimization grid-charge SOC cap now trips when the battery is being filled by grid charging, not only when solar raises the battery level. This keeps grid-charge plans aligned with the configured cap during cheap import windows.

**FoxESS and SolaX force-control baselines are preserved**
Repeated force-charge or timed force-control commands for FoxESS and SolaX now avoid clobbering the original restore baseline while a force session is already active. Restores can return to the pre-force operating state instead of treating a re-issued force command as the new normal.

Update available via HACS
