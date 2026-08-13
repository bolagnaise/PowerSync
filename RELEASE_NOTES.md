<!-- release: v2.12.1087 -->

## What's Changed

**Keep Cheapest EV plans in free grid periods while the battery charges**

Smart Schedule now uses the Home Power site import limit when sharing a free grid period with battery charging. This prevents a stale 7.4 kW planner fallback from deleting feasible free-grid windows, switching the displayed plan to Solar, and stopping the EV during the free period.

**Isolate EV regression tests from execution order**

EV action tests now reset their mutable runtime state between cases, and the vehicle-capacity tests provide their own Home Assistant registry stubs. This removes the order-dependent 5 A charger-cap failure and ensures each module passes independently as well as in the combined EV suite.

Update available via HACS
