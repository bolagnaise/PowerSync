## What's Changed

**SAJ H2: Force charge, force discharge, set idle, and restore normal fully working**
Complete rewrite of the SAJ H2 control flow based on live testing against the stanus74 integration. Force charge now sets both `passive_bat_charge_power` and `passive_grid_charge_power` registers — the grid-side register gates actual power flow independently of the battery setpoint, so omitting it was limiting charge rate to ~5 kW. Force discharge enables passive discharge mode with both battery and grid discharge registers at full rate. Set idle enters passive charge mode with all power registers zeroed, preventing the TOU schedule from driving any discharge. Restore normal turns off the passive discharge switch, which causes stanus74 to write `passive_enable=0` and restore the previously-captured AppMode=1 (TOU), returning the inverter to its normal schedule-based self-consumption with zero grid export.

**SAJ H2: Fixed register write ordering**
Any write to a passive number entity resets the passive switch state as a side-effect in stanus74. All number register writes now happen before switch commands to prevent switches from being silently reset mid-sequence.

Update available via HACS
