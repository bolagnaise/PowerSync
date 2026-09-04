<!-- release: v2.12.1232 -->

## What's Changed

**Dynamic export plans no longer value stored energy from synthetic forecast padding**
PowerSync now calculates the fallback acquisition cost for already-stored energy
from the real provider price horizon, while retaining the padded 48-hour series
for LP scheduling. Extending an Amber forecast can no longer make repeated tail
padding dominate the acquisition estimate and remove otherwise economic battery
export windows.

**Optimizer DEBUG logs now expose decisive export-plan inputs**
Each completed solve emits one structured DEBUG record with acquisition-cost
provenance, real and final price arrays, export and charge masks, effective
acquisition thresholds, aggregate constraints, and resulting actions. This keeps
large diagnostics out of entity attributes while allowing adjacent plan-present
and plan-removed solves to be compared directly.

Update available via HACS
