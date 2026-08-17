<!-- release: v2.12.1123 -->

## What's Changed

**Price-Level Charging now appears in the 24-hour optimiser plan**
Upcoming Recovery and Opportunity price windows are published beside the battery plan in Home Assistant and mobile build 415. The effective planned EV load remains a solid series, while uncertain opportunities are shown separately as dashed conditional capacity.

**Only high-confidence recovery demand affects battery optimisation**
PowerSync now projects the finite energy needed to reach Recovery SOC using the configured vehicle capacity, charger capability, real slot durations, and 90% charging efficiency. The final slot may be fractional. Opportunity charging and any window with uncertain SOC, capacity, home/plug state, price coverage, battery safeguards, or control availability remain conditional and are never treated as guaranteed demand.

**EV load arbitration remains physical-loadpoint safe**
Smart Schedule and Price-Level candidates for the same charger use the larger commitment instead of being added together, while genuinely separate chargers still sum. Tesla Fleet/BLE aliases are deduplicated through the existing physical identity mapping. A configured external planned-EV-load entity remains authoritative and suppresses internal expected demand.

**Live charging control is unchanged**
The existing 30-second controller remains the only path that can start, stop, or own a charger. Demand windows, manual stops, force modes, active ownership, no-grid-import, and home-battery preservation continue to fail closed. Projection errors fall back to the existing Smart Schedule plan, and saving Price-Level settings only schedules a coalesced background replan.

**Versioned, backward-compatible plan metadata**
The optimisation API retains `schedule.ev_charging_w` as the exact aggregate load used by the optimiser and adds optional schema-v1 projection components and classified windows. Older clients continue to work unchanged, and newer clients ignore absent, malformed, or unknown-version metadata.

Update available via HACS
