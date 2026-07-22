<!-- release: v2.12.908 -->

## What's Changed

**Solcast planning now distinguishes zero generation from stale data**
PowerSync now treats a current Solcast forecast containing zero generation as valid planning data, while rejecting forecast periods that do not overlap the active optimisation horizon. The optimizer warning now reports unavailable current forecast data instead of incorrectly saying no provider is configured.

Update available via HACS
