<!-- release: v2.12.895 -->

## What's Changed

**Tesla force-charge refreshes no longer repeat at the same tariff boundary**
When a cached optimizer action starts a Tesla force-charge window and the fresh optimization finishes a few seconds later with the same plan, PowerSync now reuses the already-valid aligned tariff instead of repeating the operation-mode, reserve, grid-charging, and tariff writes. Missing or near-expiry tariff windows and genuine five-minute plan extensions still refresh normally.

Update available via HACS
