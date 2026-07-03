<!-- release: v2.12.758 -->

## What's Changed

**Fix SolarEdge curtailment during force dispatch**
SolarEdge active-power curtailment is now skipped or released while PowerSync is force charging or force discharging the battery. This prevents a low feed-in tariff curtailment check from leaving the inverter at a 0% active-power limit while a planned or manual force charge/discharge command is active.

**Restore SolarEdge output before dispatch commands**
PowerSync now restores any existing SolarEdge active-power curtailment before sending SolarEdge force charge or force discharge commands, including optimizer-driven hardware refreshes and manual service calls.

Update available via HACS
