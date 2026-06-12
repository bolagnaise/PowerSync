<!-- release: v2.12.623 -->

## What's Changed

**Fix Sigenergy force discharge exports**
PowerSync now writes Sigenergy's signed active power target during force-discharge commands before raising the grid export limit. This lets DISCHARGE_ESS request actual battery export instead of only increasing the export ceiling while the battery continues serving local load.

Update available via HACS
