<!-- release: v2.12.1146 -->

## Fixes EV charging being slowly ratcheted down to nothing

If your site sits at its grid import limit while the home battery is charging, PowerSync could walk your EV's charge rate down one amp every thirty seconds until it hit the minimum — never settling. On a live system this took a car from 25 A to 19 A in two minutes and would have kept going.

**Update if you are on 2.12.1143, 2.12.1144, or 2.12.1145.**

### What was happening

Once PowerSync has learned how much power your battery is actually accepting, it reserves that measured intake plus a small margin so the battery has room to grow back into if it recovers.

On a site that is *not* import-limited that margin is harmless — it comes out of spare headroom. But when the import limit is already saturated, the margin can never be filled, so it read as a permanent shortfall. Every cycle the EV gave up an amp; the battery immediately absorbed the freed power; the reserve rose to match the battery's new intake; and the identical gap reappeared. The car kept paying for a gap it could never close.

The margin can still take up genuinely spare headroom, but it can no longer push the car below the rate it is already charging at. A shortfall bigger than the margin is a real one, and the EV still yields for it — battery priority is unchanged.

This surfaced because 2.12.1143 gave the battery reserve a real target for charging sessions that previously carried none. If you are on 2.12.1142 or earlier you were not affected.

### Also fixed: EV counted twice in the optimizer plan

2.12.1144 added the EV as a decision variable in the optimizer so the car and the battery are scheduled against one import limit. PowerSync has always *also* folded planned EV charging into its load forecast — so for one release the car was counted twice, and the battery's plan under-used the import headroom actually available.

The two now supersede each other: when the solver co-optimizes the car, the load overlay is skipped. This mirrors the exclusivity PowerSync already enforced when you point it at an external planned-EV-load sensor.

### Diagnosing it

With debug logging on, the dynamic EV line shows the reserve and the allowance driving each decision:

```
Dynamic EV: battery=-13.9kW, grid=15.9kW (max=16.1kW), headroom=0.2kW,
            available=0.0kW, battery_reserve=14.2kW, current=19A, target=19A,
            battery_acceptance_learned=True
```

`available` at or above zero with the battery at its learned reserve is the fixed behaviour — previously this sat at `-0.1kW` indefinitely and shed an amp every cycle.
