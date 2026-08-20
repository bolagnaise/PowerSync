<!-- release: v2.12.1170 -->

## What's Changed

**EV charging is included in the battery SoC and grid forecast again**
Planned EV power was being solved, but the final schedule serializer dropped
it before the optimizer graph and reconstructed grid flows were published.
The canonical schedule now keeps EV charging through solve, reconciliation,
and API serialization, so the SoC curve and import/export forecast account for
the car's load.

**The battery optimizer now follows EV Smart Schedule settings**
Each vehicle is co-optimized with its own charging windows, deadline, charger
limits, and current per-day policy. Minimum battery to start, Consume Battery
and its floor, Stop at Battery Floor, Preserve Home Battery, Limit Grid Import,
maximum grid price, demand-window blocks, and solar-only exceptions now form
hard planning constraints instead of being left for the runtime controller to
discover after the battery plan was already made.

**Fallback plans and diagnostics keep the same EV safety policy**
Greedy and self-consumption fallback schedules now retain per-vehicle source
rules and battery floors. Reconciliation no longer re-labels an EV's energy as
grid, solar, or battery when that source was disallowed, and policy diagnostics
show current and future blocked/eligible segments for troubleshooting.

Update available via HACS
