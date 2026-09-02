<!-- release: v2.12.1225 -->

## What's Changed

**Static tariff cost tracking after startup**

PowerSync now makes a saved static tariff available before its first energy-coordinator refresh and preserves that schedule while the full integration runtime is assembled. Existing AGL and other supported static-tariff entries no longer create avoidable unpriced startup intervals that can leave Daily Import Cost and average-cost sensors as `Unknown` for the day or month.

Cost coverage remains intentionally fail-closed: PowerSync does not invent prices or retroactively price genuinely uncovered energy intervals.

Update available via HACS
