<!-- release: v2.12.1144 -->

## The optimizer now plans your EV and your home battery together

Smart Optimization has never known about your car. The home-load forecast deliberately strips EV charging out of the history it learns from, and the solver had no EV variable — so it sized battery charge windows against your entire grid import limit while the car quietly took a share of it. The battery then got throttled to keep the site under its meter limit and missed its charge target.

The previous release stopped the car from *stealing* the battery's headroom at runtime. This one fixes the plan itself: the car and the battery are now scheduled together against one import limit.

### What this changes

If you have Smart Schedule EV charging configured, every vehicle with outstanding energy and a charging deadline now contributes its **physical charging envelope** to the plan — from now until its deadline, at your charger's rate. The solver picks the timing inside that envelope.

Two things follow from that:

- **Battery windows are sized correctly.** A plan that needed four hours at full rate but was quietly competing with the car now either takes the rate that actually fits, or spreads across more slots. No more silent shortfall at the end of a cheap window.
- **The car moves to the cheapest slots that fit.** Because EV energy is priced through the same grid import the battery uses, the solver shifts charging to where it is cheapest alongside the battery — rather than the EV planner picking a window and the battery discovering the conflict later.

Multiple vehicles combine into a single site demand. Per-car allocation stays with the EV controller, which already shares a site budget between loadpoints.

### Deliberately conservative

- **Delivery is a soft target.** If your car physically cannot finish before its deadline, the plan delivers everything it can and logs the shortfall. It never makes the whole optimization infeasible — one unreachable car target must not take the rest of your schedule down with it.
- **The plan is a ceiling, not a setpoint.** Live rate control can still charge below the planned figure for site conditions, and starting and stopping stays with the EV planner. A stale or missing plan can never strand a plugged-in car at zero amps.
- **The fallback solver still accounts for the car.** If the LP solver is unavailable, the greedy heuristic cannot co-optimize, so it treats the car as known load charging as early as its window allows. You lose the cost-shifting, but the battery plan stays correct.
- **Nothing changes without an EV.** Solves with no EV demand produce exactly the same model and the same plan as before.

### Diagnosing it

Schedule slots carrying planned EV power now report `ev_charge_w`, and the dynamic EV debug line shows the ceiling it is following:

```
Dynamic EV: battery=-8.6kW, grid=16.1kW (max=16.1kW), battery_reserve=8.6kW, ev_plan=7.0kW, ...
```

Known gap: the self-consumption hold path does not carry a planned EV figure, so during a hold the EV controller uses its own reactive rate control.
