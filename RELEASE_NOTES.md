## What's Changed

**Battery Health: Correct BMS Data Source for Pack Capacity**
The battery health calculation was reading `components.msa` as the primary source for pack energy, but that array contains all component types — PVS inverters, PVAC units, TESYNC modules, and more — most of which report zero BMS energy. This caused the health percentage to be wrong or zero for many systems. The fix now reads `control.systemStatus.nominalFullPackEnergyWh` as the primary aggregate (matching the cloud worker) and uses `control.batteryBlocks` for the authoritative pack count. Per-pack breakdown still uses `components.msa` but filters to only entries with actual BMS energy signals.

Update available via HACS
