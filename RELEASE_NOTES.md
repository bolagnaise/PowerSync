<!-- release: v2.12.1156 -->

## Smart Schedule planned your EV at a fraction of its real charge rate

**Update if the optimiser graph shows a planned EV charge well below what your charger can actually deliver — for example 3.4 kW or 1.2 kW where you expect 7.4 kW.**

The number the optimiser graph draws for planned EV charging comes straight out of the Smart Schedule plan. Two separate faults were shrinking it, and both of them are about vehicles that are *not* currently on the charger.

### A car that is away planned at 5 A

Smart Schedule regenerates each vehicle's plan *before* it checks whether that vehicle is home and plugged in. That is deliberate: a car that is out driving but due to leave at 7 am tomorrow should still reserve its overnight charge in the optimiser's load forecast, so the house battery is not spent on something else first.

For a Tesla, the plan asks the live charger resolver what the attached wall connector can deliver. When the car is away, unplugged, or simply asleep, there is no attached charger to read — so the resolver returns its safe fallback of 5 A. That fallback exists for a good reason: if PowerSync cannot identify the EVSE, it must not *command* more than the smallest current any charger will tolerate.

But a safety floor for issuing commands is not a forecast. The plan was being built at that 5 A floor, which is 1.15 kW on single phase and **3.45 kW on three phase** — for a loadpoint configured at 32 A. So the very plan that exists to protect tomorrow's charge was reserving a fraction of the energy it actually needs, spreading itself across far more hours than necessary, and sometimes reporting that it could not meet your departure target at all.

Planning now uses your configured charger rating whenever the live capability is unreadable. The 5 A floor still governs every command PowerSync sends, where the charger is present and its limit is known. A live limit that is genuinely *lower* than your configuration — a 15 A mobile connector, say — is still honoured for planning, exactly as before.

### A car that is away reserved capacity from the car that is home

Smart Schedule shares your site import limit between vehicles: when it plans one car, it subtracts what it has already planned for the others, so two cars charging at once cannot exceed what the grid connection allows.

That subtraction counted every enabled vehicle, whether or not it was actually there. A car parked at work all week still held its retained plan, and that plan still took capacity away from the car sitting on your wall connector. Because it applies in both directions, with two cars *neither* would plan at the charger's real rate even when only one was home.

Vehicles the executor has already determined are away or unplugged no longer compete for present site capacity. Two cars that are genuinely both home still share it exactly as they did.

**What you will notice:** the planned EV charge in the optimiser graph should now match what your charger can deliver, and overnight plans should occupy the hours they actually need rather than sprawling across the cheapest half of the night.

Neither fault could make PowerSync charge a car faster than its charger or your site limit allows — the command path was never affected, only the plan and the forecast built from it.
