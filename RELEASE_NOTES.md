<!-- release: v2.12.366 -->

## What's Changed

**Optimizer reserve floor enforcement**
The built-in optimizer now treats the configured optimizer backup reserve as a runtime floor, not just a charted LP boundary. When the plan reaches the reserve floor it holds the battery instead of leaving supported systems in natural self-consumption, so supported battery installs do not continue draining below the configured optimizer reserve.

**Live reserve and mode drift correction**
Tesla mode and backup reserve checks now prefer live Home Assistant entity state before cached Tesla site data, and the optimizer reasserts the configured floor if a runtime reserve has drifted lower. SAJ H2 and Neovolt software floors are also updated immediately when the optimizer reserve setting changes.

**Solax reserve visibility**
Solax systems now report their active minimum SOC and backup reserve into the optimizer data path, allowing PowerSync to verify and reapply reserve floors consistently.

Update available via HACS
