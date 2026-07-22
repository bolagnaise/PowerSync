<!-- release: v2.12.911 -->

## What's Changed

**Charge By Time now preserves deadlines already met at solve start**
When the battery already met the configured Charge By Time target, the optimiser could omit the future deadline floor and allow self-consumption to drain below the target. The deadline is now preserved in both the HiGHS plan and greedy fallback while retaining the existing feasibility margin, Grid Charge SOC cap, and feasible solar-refill behaviour.

Update available via HACS
