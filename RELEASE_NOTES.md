<!-- release: v2.12.594 -->

## What's Changed

**Scheduled charging no longer fights Solar Surplus sessions**
PowerSync now leaves an active Solar Surplus EV charging session under Solar Surplus ownership instead of trying to stop it as an outside-schedule external charge. This removes the confusing "Stop Scheduled External failed" status when Solar Surplus owns the loadpoint and prevents Scheduled Charging from interrupting a valid solar-driven session after the scheduled window has ended.

Update available via HACS
