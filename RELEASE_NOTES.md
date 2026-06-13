<!-- release: v2.12.636 -->

## What's Changed

**Restore Sungrow charge caps after optimizer charge windows**

PowerSync now saves the current Sungrow maximum battery charge limit before applying a temporary optimizer force-charge cap, then restores it when returning to normal or self-consumption. This prevents a completed optimizer charge window from leaving the inverter capped at the previous charge target, so later idle or self-consumption periods can still absorb available solar instead of exporting unnecessarily.

Update available via HACS
