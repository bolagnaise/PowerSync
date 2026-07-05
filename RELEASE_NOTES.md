<!-- release: v2.12.763 -->

## What's Changed

**Fix Sungrow SH restore from stale 10 W discharge caps**
PowerSync now detects and repairs a stale near-zero Sungrow discharge cap during restore to self-consumption/normal operation, even when the original pre-control discharge limit was lost after a Home Assistant reload or restart. This fixes cases where the Sungrow inverter accepted the EMS self-consumption restore writes but the battery still stayed at `0.00 kW` while the home imported from the grid because the max discharge register remained at the temporary 10 W fallback.

Update available via HACS
