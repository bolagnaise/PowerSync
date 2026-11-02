<!-- release: v2.12.1601 -->

## Optimiser events and EV stop warnings

PowerSync now publishes Home Assistant events for battery optimiser decisions and EV command results, so you can build your own safety notifications. A failed EV stop creates a persistent HA warning and a dedicated event for automations. Fresh Tesla and standalone Zaptec telemetry can also warn when charging continues at least 90 seconds after a stop request; an accepted command alone is never reported as a confirmed physical stop.

EV time-window stops no longer send a stopped notification when the stop request fails. Warnings clear after newer stopped telemetry confirms the result, and later charging sessions can raise a fresh warning. Other chargers still need a separate power-sensor check to detect an accepted request that does not take effect. Phone notifications require an HA automation; see the [event and notification examples](https://github.com/bolagnaise/PowerSync/blob/main/docs/ha-action-events.md).

Update through HACS and restart Home Assistant.
