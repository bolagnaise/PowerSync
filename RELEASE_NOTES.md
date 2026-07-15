<!-- release: v2.12.857 -->

## What's Changed

**CovaU remaining-quota sensors now load without metadata warnings**
The Free Import Remaining and Premium Export Remaining sensors now use Home Assistant-compatible energy metadata. Their kWh values and dashboard behavior are unchanged, while restarts no longer report the invalid `energy` plus `measurement` state-class warning.

Update available via HACS
