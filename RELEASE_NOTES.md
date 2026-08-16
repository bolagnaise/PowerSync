<!-- release: v2.12.1115 -->

## What's Changed

**Timed manual controls now rebuild the Smart Optimization plan**
Force Charge, Force Discharge, Hold SoC, and timed Self-Consumption immediately trigger a fresh forward plan. The active control is fixed across its timer window, its forecast SOC impact is included, and the remaining horizon is optimized from that projected battery state.

**Cancel, replacement, extension, and expiry are reflected without waiting for the next cycle**
Every external-control state change requests another immediate plan rebuild. User-owned controls continue to own hardware dispatch while active, so the optimizer updates the Action Plan without duplicating or overriding the accepted force command.

**Manual projection is explicit and remains safety-bounded**
Projected slots now expose their manual control source, action, and projection reason through the Action Plan/API. Charge and discharge power remain bounded by battery, site, network, SOC, and hardware-reserve limits; monitoring-only connection methods remain write-free. Projection metadata describes plan intent rather than claiming a new hardware acknowledgement.

Update available via HACS
