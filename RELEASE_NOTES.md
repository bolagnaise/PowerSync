## What's Changed

**SAJ H2: Fix restore_normal — discharge was locked after returning from force charge/discharge**
`restore_normal()` was calling `_set_number("discharge_power", 0)` which mapped to the `passive_battery_discharge_power` register and locked the battery at 0% discharge rate — even after passive self-consumption was re-enabled. After restoring, the battery held SOC and the house drew from the grid. Fixed: `restore_normal()` now explicitly sets `discharge_power_pct = 1100` (full self-consumption rate) before re-enabling passive mode.

**SAJ H2: Implement IDLE mode — hold battery at current SOC**
When the optimizer schedules an IDLE slot (hold charge for an upcoming price spike), SAJ H2 now sets `passive_battery_discharge_power = 0` to prevent all discharge while keeping `passive_enable = 2` so the grid charge schedule stays inactive. The battery holds its current SOC, charges freely from solar, and the grid serves home load. Exiting IDLE calls `restore_normal()` which re-enables full self-consumption.

**SAJ H2: Fix force_discharge register and scale**
`force_discharge()` was sending a watt value to `passive_battery_discharge_power_input`, a register that accepts percentage of rated power (0–1100). This could trigger HA validation errors or set the wrong rate. Fixed: force discharge now sets the register to 1100 (full rate) and relies on the discharge switch for direction control, consistent with how force charge works.

Update available via HACS
