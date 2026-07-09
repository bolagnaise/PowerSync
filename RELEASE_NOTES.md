<!-- release: v2.12.801 -->

## What's Changed

**Fixed Sungrow discharge-limit restores after Modbus retry**
Sungrow SH restore now clamps a recovered discharge-limit target back to the configured Smart Optimization maximum before retrying the inverter write. This avoids retrying an over-high stale register value after a failed restore, which could leave the battery effectively capped until the value was manually reset in iSolarCloud.

**Stabilized Powerwall local backup-reserve readback**
Tesla Powerwall local control now remembers the local reserve value it just wrote and uses that pending write to normalize the next local readback while Tesla cloud `site_info` is still stale. This keeps the Home Assistant backup reserve entity aligned with the value the user requested instead of briefly showing offset-derived values from old cloud data.

Update available via HACS
