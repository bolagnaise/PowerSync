<!-- release: v2.12.887 -->

## What's Changed

**Recover CovaU quota tracking after a cumulative meter becomes available**

PowerSync now resumes CovaU SolarMax quota settlement when a configured
`total_increasing` import or export meter is briefly unavailable during setup
and later returns with a valid nondecreasing total. The accumulated interval is
settled before confidence is restored, so the free-import and premium-export
prices no longer remain suppressed until midnight after a harmless startup
ordering delay.

Saved affected state is re-evaluated after updating and reloading. Meter resets,
corrections, unsafe first samples, and power telemetry gaps remain conservative
and cannot be cleared by a later availability update.

Update available via HACS
