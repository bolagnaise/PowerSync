<!-- release: v2.12.1166 -->

## Two-car sites: the earlier car's deadline was dropped from the plan

**Update if you have more than one vehicle managed by PowerSync and they do not share the same departure time.** Single-vehicle sites are unaffected by this one — but if you have not yet taken **v2.12.1165**, take it now regardless; it is the release that restores EV charging to the optimizer plan at all.

### One block, one deadline, two cars

PowerSync hands your vehicles to the optimizer as a single combined block, because they draw through one site import limit and the solver has to plan the home battery against what is actually left. Capability adds up per slot, and required energy adds up.

The deadlines did not. Only one delivery constraint was built, at the end of the *combined* window, so the earliest departure in the house simply stopped existing as far as the solver was concerned.

On a real two-Tesla site the effect is exact. One car due at **06:00** needing **32.9 kWh**, a second car with no departure time needing **7.6 kWh**, and a tariff whose free import window opens at **10:00**:

- **40.5 kWh** total demand, and the solver satisfied it the cheapest way it could see
- **29.4 kWh** placed in the free 10:00 window
- **15.6 kWh** delivered before 06:00 — less than half what the departing car needed

The plan was cheap and it was wrong. It showed a car charging in a window it had already left, and left it short at the time it mattered.

**What changed.** A combined plan now carries a cumulative requirement for each distinct deadline: by the earliest departure, everything owed to the vehicles leaving then must already be delivered. The solver gets one delivery row per stage instead of one for the whole horizon.

Same site, after the fix: **36.6 kWh** before 06:00 — the departing car's full 32.9 kWh once charging losses are counted — and the second car's energy takes the free window, which is where it belonged all along.

Where the optimizer coarsens time into longer blocks, a block straddling a deadline now counts only for the part that falls inside it, so a long block cannot credit energy delivered after a deadline against that deadline.

A single vehicle produces exactly one stage covering its own window, which is the model that was already there. Nothing about a one-car site changes.
