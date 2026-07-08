<!-- release: v2.12.792 -->

## What's Changed

**Optimizer boundary commands wait for fresh solves**
PowerSync now avoids issuing cached force charge, force discharge, or export commands at a schedule boundary before the fresh optimizer run has rechecked live SOC and prices. This prevents short stale force commands when battery state or a manual restore changed right before the boundary, while still letting self-consumption and restore actions run immediately.

**Battery-system switches ignore stale connection keys**
Changing the configured battery/control system now clears connection keys for other brands, and runtime setup honors the selected battery system before falling back to legacy host-key detection. This prevents stale host, station, or entity settings from starting the wrong coordinator after switching brands.

**Powerwall local mode readback**
Local Powerwall snapshots now read the operation mode from the top-level `default_real_mode` value, with the older nested location kept as a fallback, so the operation-mode select can reflect local gateway state instead of silently falling back to cloud cache.

**EPEX export-price default coverage**
Added regression coverage for EPEX setups without a configured export rate, keeping the default export value at 0 rather than treating the retail import price as feed-in revenue.

Update available via HACS
