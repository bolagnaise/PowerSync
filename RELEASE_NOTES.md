<!-- release: v2.12.550 -->

## What's Changed

**Demand charge tracking only counts billable peak-window imports**
PowerSync now updates the recorded peak demand only when the current sample is inside the configured demand-charge window. Off-peak import spikes still appear in the live grid-import reading, but they no longer inflate the estimated demand charge for tariffs where only peak-window demand is billable.

**Dashboard EV presence works for idle plugged-in PowerSync EV sensors**
The dashboard and energy-flow card now use the `is_connected` and `is_charging` attributes on the PowerSync EV power sensor as EV presence signals. This keeps a plugged-in EV visible even when it is idle and not actively drawing charging power.

Update available via HACS
