<!-- release: v2.12.1142 -->

## What's Changed

**Profit Max no longer exports morning solar it plans to buy back at a higher price**
A Sigenergy site on Amber reported a 24-hour plan that exported solar between 08:35 and 09:50 at 2.14–2.72c/kWh and then, in the same plan, grid-charged the battery at 9.64c/kWh to reach 90%. That is a guaranteed round-trip loss, and the only way to stop it was to keep switching the battery to Self Consumption by hand.

**Why it happened**
Profit Max may hold battery charging so high-priced solar exports directly, but only when a cheaper, reachable interval can put that energy back. The check that funds the hold looked at how much each later interval could physically absorb — not at how much of that capacity the rest of the plan already needed. On a day where the battery is charge-capacity constrained rather than solar constrained (a large battery starting low, a modest solar day), the same midday solar was counted twice: once as the charge the plan was always going to do, and again as the deferral's replenishment. The hold is applied before the plan is solved, so the optimizer could not reject the trade and covered the shortfall with grid import instead.

**The fix**
Every hold is now re-checked against the plan that was actually produced. If the plan grid-charges later in the horizon at more than the feed-in price the hold was selling at, that hold is released and the plan is re-solved without it. Holds whose feed-in price still beats the plan's own grid-charge cost are kept, so a genuine high-feed-in deferral behaves exactly as before. Released holds appear on `sensor.power_sync_optimization_status` under `profit_max_solar_export.capability.post_solve_revision` with reason `grid_replenishment_costlier_than_export`.

Verified against the solver on the reported case: the corrected plan reaches the same end-of-day state of charge with the whole import/export spread recovered, and a high-feed-in control case keeps all of its holds and still comes out ahead.

Update available via HACS
