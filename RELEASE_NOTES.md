<!-- release: v2.12.548 -->

## What's Changed

**Sungrow optimiser charge recovery**
PowerSync now detects when a Sungrow system is still in self-consumption or otherwise not accepting an optimiser-owned force-charge command while the LP schedule still wants charging. The optimiser will refresh the Sungrow force-charge command instead of leaving the dashboard showing `charge` while the battery continues discharging.

**Sungrow EMS telemetry support**
The force-charge health check now understands Sungrow EMS mode and charge command telemetry, including the forced-charge command state. This avoids unnecessary rewrites when the inverter is already in forced charge, while still recovering from a dropped or stale hardware command after a reload.

Update available via HACS
