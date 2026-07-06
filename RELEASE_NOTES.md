<!-- release: v2.12.779 -->

## What's Changed

**Repair Sungrow self-consumption when reserve blocks discharge**
PowerSync now detects when a Sungrow inverter is in self-consumption but still importing to cover home load because the inverter reserve/min-SOC is stuck above the cached hardware reserve. When this condition is detected, PowerSync reapplies self-consumption and writes the cached reserve back to the inverter so the battery can resume covering home load.

Update available via HACS
