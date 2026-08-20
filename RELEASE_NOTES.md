<!-- release: v2.12.1165 -->

## Smart Optimization plans stopped accounting for EV charging

**Update if you use Smart EV Charging and your optimizer plan stopped showing the car.** If the SOC and Battery Power chart lost its EV Charging series some time after **v2.12.1146**, and the plan started reading as though no vehicle existed, this is why. Sites with no EV are unaffected.

The optimizer was doing the work correctly the whole time. The answer was being deleted on the way out.

### The car was planned, then erased before you saw it

PowerSync hands your vehicles' outstanding energy to the main optimizer as one combined block, and the solver places it against your real forward prices and your site import limit — the same shared envelope the home battery charges through. On a reporting site the debug log showed exactly that, every cycle:

```
EV load overlay: superseded by LP co-optimization (39.7 kWh for _combined)
EV demand in this solve: 39.7 kWh for _combined
```

39.7 kWh across two vehicles, accepted by the solver, placed in full — no shortfall reported.

After the solver finishes, a reconciliation pass rewrites every slot in the plan so the published schedule describes what the hardware will physically do rather than what the model wanted. That pass rebuilt each slot from a fixed list of fields, and the list was written before the planned EV draw existed. Every slot therefore came out of reconciliation with its EV figure reset to zero — the entire 39.7 kWh, on every cycle, immediately after being solved. A second pass that trims export slots did the same thing.

Between **v2.12.1144** and **v2.12.1146** this only lost a label, because the plan still drew the car from a separate load overlay. **v2.12.1146** fixed a genuine double-count by making those two sources mutually exclusive: whenever the solver co-optimizes the car, the overlay is deliberately blanked. From that release on, the blanked overlay and the zeroed figure were the only two sources the chart had, so the car vanished from the plan entirely.

### It also cost you rate control

The same figure is what the EV controller follows to share one import envelope between the car and the battery. Reading zero, it concluded the optimizer was not modeling EV demand at all and fell back to its own reactive rate control — still safe, still inside your import limit, but no longer charging to the plan the solver had built.

**What changed.** Both passes now restamp the slot they were given instead of rebuilding it from a list, so every value they do not explicitly change survives by construction. This is deliberately structural: the same mistake cannot be made again by adding a field and forgetting a rebuild site.

### A second route to the same blind spot

When the solver cannot satisfy your constraints, PowerSync falls back to a self-consumption hold — the do-no-harm behaviour your inverter shows without optimization. That fallback ignored the EV plan outright, so it simulated a house that charges no car: the SOC trajectory and predicted cost understated the drain, and the plan it emitted again carried no EV demand for the controller to follow.

The other fallback already handled this, folding the car in at its earliest-possible draw. Both now behave the same way. This path was not involved in the reported fault — it only runs on an infeasible solve — but it reached the same blind spot by a different route.
