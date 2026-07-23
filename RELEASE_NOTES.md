<!-- release: v2.12.920 -->

## What's Changed

**EV deadline charging now starts at the exact calculated time**
Time-critical charging plans now trim the first forecast window to the energy
actually required instead of reserving the entire hour. The start time uses the
configured charger power and vehicle capacity, so a plan no longer begins early
because of a separate hard-coded 7 kW estimate and 20% buffer.

**Planning, execution, and optimizer demand stay aligned**
PowerSync now executes only inside the published deadline windows, including
when a target cannot be met, so demand-window exclusions are not bypassed.
Partial charging intervals are prorated in the battery optimizer, and repeated
daylight-saving hours retain their correct chronological order.

Update available via HACS
