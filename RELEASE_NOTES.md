<!-- release: v2.12.621 -->

## What's Changed

**Clearer AC inverter curtailment status**
PowerSync now tracks the active AC inverter control mode separately from the generic curtailed state. The inverter status sensor can show Normal, Load Following, Shutdown, or Curtailed, and exposes the current control mode and target power so dashboards can show what PowerSync is actively doing.

**Faster Enphase DPEL refresh**
Enphase DPEL re-apply checks now run every 15 seconds while load-following or shutdown curtailment is active. Other inverter brands keep their existing 30-second effective update cadence, so this tightens Enphase control without increasing the general polling rate.

**Scheduled EV charging minimum-current guard**
Scheduled dynamic EV charging now preserves the minimum charging current when the battery is full and the site is near the configured grid import cap. Non-scheduled sessions can still stop when the same grid-cap condition leaves no usable headroom.

Update available via HACS
