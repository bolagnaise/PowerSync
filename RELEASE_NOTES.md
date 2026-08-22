<!-- release: v2.12.1181 -->

## What's Changed

**Retain Demand Charge peaks across an options reload**
Peak Demand This Cycle now persists through a same-cycle Demand Charge disable/re-enable or other structural options update. The tracker restores only a peak recorded for the current billing cycle with the same demand window, so a new billing cycle or changed window starts fresh instead of carrying forward a stale value.

**Keep Demand Charge runtime in sync with API changes**
Demand Charge changes made through the provider-config API now use the normal reload lifecycle, keeping its tracker, sensors, demand protection timers, and optimizer settings aligned.

Update available via HACS
