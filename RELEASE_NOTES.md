## What's Changed

**Fix: Optimizer oscillation causing rapid force_charge↔restore cycles**
Amber sends two price coordinator updates per 5-minute window (usage price + spot price). Without rate-limiting, both updates triggered a full LP solve, allowing two consecutive CHARGE decisions to satisfy the holdoff counter within seconds. This caused force_charge and restore_normal to alternate rapidly throughout the night when the LP was near a charge/idle decision boundary — each cycle hitting the inverter with a full passive mode switch. On the SAJ H2 this was enough to trip the battery BMS protection. Fixed by rate-limiting price-triggered LP runs to at most one per optimizer interval (5 minutes), matching the cadence of the polling loop.

Update available via HACS
