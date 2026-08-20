<!-- release: v2.12.1168 -->

## The plan's battery draw, grid flows and SOC now account for EV charging

**Update if you use Smart EV Charging.** This completes the series started in v2.12.1165: the car was restored to the plan, then to each vehicle's deadline — but the plan's *physics* still described a house with no car. If your chart showed the EV charging while the battery draw, grid import and SOC curve carried on as if it weren't there, this is the release that fixes it.

### One convention violation, replicated at every stage

Since v2.12.1144 the solver models your car as its own decision variable. Its power balance is right: in every slot, grid import minus export equals house load **plus the car** minus solar, plus battery charge minus discharge.

But the car's draw is carried on the schedule itself, not in the load forecast — and every pass that runs *after* the solver recomputed physics from solar and load alone. Each one quietly erased the car from a different part of the published plan:

- **Grid flows** — published grid import was understated by the car's entire draw (or export overstated), and the plan's predicted cost, recomputed from those flows, omitted the car's import cost entirely.
- **Battery draw and SOC** — the reconciliation pass caps natural discharge at house-minus-solar. A battery the solver planned to discharge into the car overnight was clipped back to house-only service and restamped with a nearly flat SOC — while the real battery, in self-consumption, will feed the car and drain hard. This is the flat battery line under a charging car.
- **Action labels** — slots were classified against house-only load while the solver's grid import included the car, so a slot where only the car imports could be labelled as battery grid-charging.
- **Export accounting** — discharge that serves the car was reported as battery-to-grid export, and the ZeroHero-style bonus passes valued that phantom export against your capped quota.
- **Savings** — the "what you'd pay without a battery" baseline had no car in it, so the EV's unavoidable import cost read as money Smart Optimization was losing you.
- **No Idle conversions and free-import quotas** — idle slots converted to self-consumption sized their discharge without the car, and the free-window quota estimate ignored the car drawing through the same cap.

### What changed

One rule now, enforced at the point every schedule is built: load arrays are house-only everywhere, the EV draw lives on the schedule, and every derivation adds it back explicitly. The fallback solvers (greedy, and the hold used when a solve is infeasible) follow the same contract.

### Why this is the last release in this series

Each earlier fix moved the gap one stage downstream, so this one is pinned differently: the test suite now asserts **per-slot energy conservation over the published plan** on all three solve paths — grid import minus export must equal house-plus-car load minus solar, plus charge minus discharge, in every slot. A future pass that drops the car breaks conservation and fails the build, whichever stage it hides in.

One number worth knowing: with the battery serving a charging car, the plan previously accounted for 4.0 kWh of supply against 14.0 kWh of demand — 10 kWh of the car's energy came from nowhere. It now balances to within 50 W in every slot.
