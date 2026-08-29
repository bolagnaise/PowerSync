<!-- release: v2.12.1207 -->

## What's Changed

**Site-local grid-charge blackout windows**
Smart Optimization can now exclude one or more local-time windows from optimizer grid charging. The setting is available during setup, in options, and through optimizer settings; existing installations continue with no blackout windows by default.

**Reliable overnight and timezone-aware scheduling**
Windows can cross midnight, such as 22:00–06:00. PowerSync evaluates each generated schedule timestamp in the site's local timezone, so schedule slots remain contiguous through daylight-saving transitions.

**Safe charge planning and execution**
Blackout windows combine with existing grid-charge price, quota, state-of-charge, and battery constraints without preventing normal solar charging, self-consumption, or discharge. Optimizer-owned force charging is also restored to normal operation at a blackout boundary, while manually owned force charging is left unchanged.

**Clear Charge By Time visibility**
Charge By Time targets remain constrained by blackout windows. When a target would otherwise be feasible, optimizer status identifies blackout windows as the reason it cannot be met and includes the affected slots and deadline context.

Update available via HACS
