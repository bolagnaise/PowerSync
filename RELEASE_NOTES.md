<!-- release: v2.12.813 -->

## What's Changed

**Restore Tesla backup reserve after IDLE holds**
PowerSync now keeps Tesla Powerwall IDLE holds separate from the user's real backup reserve. If reserve readback is temporarily unavailable or a restore command fails, PowerSync retains the original restore target and retries instead of adopting its own elevated IDLE reserve. This prevents Powerwalls remaining pinned at the held state and importing from the grid after the optimizer returns to Self Consumption.

Update available via HACS
