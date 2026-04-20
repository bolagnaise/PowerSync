## What's Changed

**GoodWe (and Other Non-Tesla Batteries): Integration Crash Fixed After Monitoring Mode Disabled**
When monitoring mode was turned off on a GoodWe, FoxESS Modbus-only, or any non-Tesla setup, the integration failed to restart with a `TypeError`. The tariff sync function fell through to Tesla-specific API code that assumed a token getter was always present. A null guard now exits cleanly for non-Tesla systems before reaching that code.

**GoodWe / Non-Tesla: Spurious "Missing Tesla Site" Error Suppressed**
`set_grid_export` and `preserve_charge` actions were being called for non-Tesla systems (including GoodWe) despite being Tesla-only operations. These calls now exit early with a debug log instead of propagating to the service handler and logging a false error.

Update available via HACS
