<!-- release: v2.12.821 -->

## What's Changed

**Steadier curtailment and spike responses at price boundaries**
When the export price hovered right at the curtailment threshold (~1c/kWh) or the AEMO price sat at your spike threshold, PowerSync could rapidly toggle between curtail/restore or spike-enter/exit on every price tick — hammering the inverter with Modbus writes or Tesla with tariff uploads. Both decisions now use hysteresis: they engage at the same thresholds as before but only release after the price moves decisively away, eliminating the flapping.

**More accurate export reserve planning**
Two fixes to how PowerSync reserves battery for home load between export windows: an export window split by a brief dip no longer double-counts home load (which over-held the battery and cost export revenue), and the reserve calculation now recognises paid-to-import (ZeroCharge-style) windows as cheap recharge opportunities instead of over-reserving past them.

**Stale schedules are now visible instead of silently acted on**
If optimization stopped producing fresh schedules (for example after a swallowed solver failure), PowerSync previously kept executing the last schedule's final action indefinitely while reporting "active". The current action now expires once the schedule ends, and the optimization status reports "stale" with a new schedule age field so you can see the problem in the app or dashboard.

**Multi-EV households: fixed a lease leak that could block charging**
A charging lease claimed under the default vehicle slot but released under a specific VIN could linger and wrongly block another vehicle from starting. Lease release and clear now clean up the default slot the same way claiming does.

**EV load no longer double-counted**
If both an external planned-EV-load entity and PowerSync's internal EV planner were configured for the same vehicle, both loads were added to the forecast, causing over-charging and over-importing. The external entity now takes precedence, with a warning logged when both are set.

**Sigenergy: export limit protected across reloads**
Reloading PowerSync mid-curtailment could capture the temporarily-curtailed 0W as the "original" export limit, and a later restore would remove your DNSP export cap entirely. A curtailed zero is no longer accepted as the original limit.

**Reliability and forecasting refinements**
A calibration-recovery timer is now cancelled on reload instead of leaking against the new instance; solar nowcast suppression from cloudy evenings now resets overnight instead of dampening the next morning's forecast; schedule updates apply atomically; time-window features (Happy Hour, Export Boost) align exactly to price-slot boundaries; the LP solver now accepts a near-optimal solution on time-limit instead of falling back to the simpler heuristic; and per-slot price lookups now work for dynamic price providers.

Update available via HACS
