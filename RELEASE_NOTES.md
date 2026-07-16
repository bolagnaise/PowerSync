<!-- release: v2.12.866 -->

## What's Changed

**Charge By Time no longer cuts short profitable exports**
PowerSync now applies the grid-charge SOC cap chronologically instead of treating the battery's initial headroom as a single budget for the whole plan. Energy exported before a Charge By Time deadline reopens charging headroom, allowing the optimizer to preserve the full profitable export window and then refill in the cheapest eligible slots up to the configured cap.

**Grid and solar charging remain correctly separated**
The LP solver, greedy fallback, emitted schedule, and result reconciliation now enforce the same per-slot rule: grid energy cannot take the battery above the grid-charge cap, while forecast solar can still charge beyond it.

Update available via HACS
