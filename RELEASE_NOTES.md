<!-- release: v2.12.1201 -->

## What's Changed

**Demand Charge peaks now fail safely when persistence is temporarily unavailable**
If PowerSync observes a new Demand Charge peak but Home Assistant cannot save it, Peak Demand This Cycle and its estimated cost now become unavailable rather than presenting an unsettled value. PowerSync retains the observed peak and retries saving it on the next update, even when there is no larger sample. Once persistence succeeds, the retained same-cycle peak is restored to the sensors.

**Monitoring Mode now blocks every automatic solar-curtailment command route**
Periodic, startup, WebSocket, and fast load-following curtailment checks now stop before battery or inverter controller commands when Monitoring Mode is active. The checks remain visible as Monitoring Mode decisions, but PowerSync does not issue a curtail or restore write.

Update available via HACS
