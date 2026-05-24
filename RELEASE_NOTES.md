<!-- release: v2.12.461 -->

## What's Changed

**Solar surplus EV delay visibility**
Loadpoint status now exposes start and stop delay timers for solar surplus charging. Dashboards and clients can show when PowerSync is waiting for sustained surplus before starting, or waiting out the stop delay before turning a charger off.

**Dashboard card drag handle**
Custom dashboard layout editing now starts drag gestures from a dedicated Drag handle instead of the whole card. This keeps normal mobile scrolling and card interaction from being captured while customization mode is active.

**Price forecast boundary alignment**
The optimizer now places timestamped import and export price intervals into their real forecast slots, then fills gaps without shifting later tariff boundaries. This prevents missing leading intervals from moving a later high-price boundary into the wrong optimizer slot.

**SolarEdge curtailment status**
SolarEdge inverter status no longer inherits the Fronius simple-mode stale curtailment fallback. If SolarEdge reports a 100% active power limit and is still producing, PowerSync will show it as running instead of incorrectly keeping a curtailed state.

Update available via HACS
