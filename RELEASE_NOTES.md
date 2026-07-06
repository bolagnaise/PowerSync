<!-- release: v2.12.778 -->

## What's Changed

**Restore Sungrow self-consumption discharge at low household load**
PowerSync now detects SH10RS/SBH systems that report self-consumption while the battery remains at 0 W and the grid is carrying the small house load. The restore path no longer waits for a high import threshold before repairing a stale Sungrow discharge cap, so low overnight loads are handled as well.

**Reapply Sungrow restore when the optimiser sees blocked discharge**
Smart Optimisation now uses the same blocked-discharge telemetry check before skipping a repeated self-consumption action. If the plan expects the battery to cover load but the inverter is still not discharging, PowerSync reapplies the Sungrow restore instead of assuming the previous self-consumption command is still effective.

Update available via HACS
