## What's Changed

**SAJ H2: Fix restore_normal causing grid import**
After force charge/discharge completed, `restore_normal()` was turning off `passive_charge_control` which released the inverter to follow its configured charge schedule (working mode 4), causing it to import from the grid at full home load. The correct idle state for SAJ H2 is `passive_charge_enable = 2` (passive self-consumption mode), which tells the inverter to operate from available solar/battery rather than following the grid charge schedule. `restore_normal()` now resets power targets to 0, turns off passive discharge, and sets `passive_charge_enable_input = 2`.

**SAJ H2: Fix entity discovery never running — sensors stuck at 0.0**
`SajH2EnergyCoordinator` was calling `get_status()` without ever calling `connect()`, so `_entity_map` was always empty and all sensors reported 0.0. Entity discovery now runs lazily on the first update.

Update available via HACS
