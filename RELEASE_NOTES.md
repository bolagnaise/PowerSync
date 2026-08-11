<!-- release: v2.12.1064 -->

## What's Changed

**Keep Solar Surplus status aligned during active curtailment**

PowerSync's normalized EV loadpoint status now carries the inverter's live
curtailment state into the same surplus calculation used by the active EV
controller. When the home battery is full and zero-export curtailment is
active, the dashboard no longer shows the configured household buffer as
withheld while the controller correctly makes that power available to the EV.

The curtailment value is normalized fail-closed before it reaches the status
calculation. This is a dashboard/status correction only: EV charging commands,
battery reserve behavior, and existing grid-import protections are unchanged.

Regression coverage verifies the normalized loadpoint endpoint with active,
inactive, missing, and malformed curtailment values.

Update available via HACS
