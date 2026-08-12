<!-- release: v2.12.1081 -->

## What's Changed

**Correct per-vehicle Smart Schedule energy estimates**
Smart Schedule previews now resolve state of charge through the same vehicle-scoped identity and cache used by automatic execution. Multi-EV systems no longer risk calculating one vehicle's plan from another configured BLE vehicle's battery level, while manual usable-capacity overrides remain authoritative.

**Prefer guaranteed free grid windows when no departure is set**
Solar Preferred schedules without a departure deadline now use cost-ranked planning, place free or negative-price grid ahead of forecast solar, align tariff windows to their real wall-clock boundaries, and respect the home battery optimiser's allocation and site import limit before offering EV capacity.

Update available via HACS
