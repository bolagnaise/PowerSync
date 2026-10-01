<!-- release: v2.12.1340 -->

## What's Changed

**Expired Tesla BLE power no longer asserts active charging**

Positive BLE watts whose measurement timestamp has expired are now treated as unavailable instead of being reused to claim that the vehicle is charging. PowerSync keeps the loadpoint conservative and unknown until fresh charging state or power telemetry arrives, so Home Load is not calculated from an expired EV reading.

**External ownership follows usable telemetry**

Observed Tesla sessions no longer create an external ownership lease from a positive BLE value that has already failed the freshness check. Fresh charging-state telemetry and the existing stopped-state protections remain covered by regression tests.

Update available via HACS
