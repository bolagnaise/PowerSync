<!-- release: v2.12.1174 -->

## What's Changed

**Cheapest Period now starts at the front of equal free windows**
When several slots in a free charging period have the same price, PowerSync now
prefers the earliest available slots. This keeps a planned 10:00 start at 10:00
instead of allowing the optimizer and dashboard to shift the same energy toward
the end of the free period.

**Dashboard and live EV charging now follow the same per-vehicle plan**
Smart Schedule now distinguishes an explicit 0 kW optimizer slot from a missing
or stale plan. A current 0 kW slot pauses charging while retaining schedule
ownership for a later restart, and positive slots act as safe charging ceilings
across battery-target and Solar Surplus control.

**Safer Tesla low-current and multi-vehicle control**
Optimizer power is converted to current without exceeding the planned ceiling,
including 1 A operation when the selected Tesla Home Assistant add-on exposes
that supported range. Multi-Tesla sites also reallocate shared headroom around
each vehicle's exact plan instead of applying a fleet total to one car.

Update available via HACS
