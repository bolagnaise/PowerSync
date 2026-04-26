## What's Changed

**SAJ H2: Fix force_discharge — battery was staying idle instead of discharging**
`force_discharge` was calling `turn_off(passive_charge_control)` as a final step to prevent grid charging. Testing revealed that any write to `passive_charge_control` — even to OFF — triggers the stanus74 integration to reset `passive_discharge_control` back to OFF as a side effect. This left the battery with neither switch active, so the inverter sat idle with the grid supplying the full home load. Fixed by removing the final `turn_off(charge_switch)` call: `passive_enable=2` already resets both switches to OFF at the start, and turning `passive_discharge_control=ON` leaves `passive_charge_control` safely OFF without any additional write.

Update available via HACS
