# Optimiser and EV action events

PowerSync publishes the following events on the Home Assistant event bus. Listen in **Developer Tools → Events**, or use an event trigger in an automation. Each event includes `schema_version: 1`, `entry_id` and a UTC `timestamp`. Filter by `entry_id` when more than one PowerSync instance is configured.

| Event | Meaning |
| --- | --- |
| `power_sync_optimizer_action` | A battery optimiser action is being evaluated, Monitoring Mode is blocking it, its internal runtime target is recorded, or execution raised an error. |
| `power_sync_ev_command` | An EV ownership/command result was recorded, or newer telemetry confirmed a stopped state. |
| `power_sync_ev_stop_failed` | A recorded stop failed, or fresh Tesla/Zaptec telemetry still reported charging at least 90 seconds after a stop. |

Battery action payloads include `planned_action`, `effective_action`, `power_w`, `status` and `physical_confirmation: false`. Status is `evaluating`, `monitoring`, `runtime_target` or `failed`. `effective_action` is null during initial evaluation or an unhandled failure. `power_w` is the planned power, which can differ from the command after runtime safety limits. A runtime target is coordinator metadata; it does not establish that a hardware command was sent, accepted or took effect. Safety and ownership deferrals can emit only `evaluating`. Events describe current execution evaluations, rather than every future slot of a plan or every individual hardware write.

EV payloads include `vehicle_id`, `command`, `source`, `success`, `reason`, `record_type`, `physical_confirmation` and `observed_at`. `record_type` distinguishes `command`, `ownership` and `observation` records. The vehicle ID is the existing ownership/loadpoint ID; shared charging profiles use their physical loadpoint ID. Command names also include ownership transitions such as `release`; success on such a record does not mean a physical stop was attempted. Even a successful `stop` request remains `physical_confirmation: false`. Only `command: stop_observed` from newer stopped telemetry has `physical_confirmation: true`, with `observed_at` showing when that telemetry was captured. Neither a stopped vehicle state nor an acknowledgement proves measured zero power for every provider.

A failed-stop event also creates a persistent **PowerSync: EV stop needs attention** notification in HA. Repeated failures share one warning per instance/loadpoint until newer stopped telemetry clears it. A successful retry alone does not clear the warning. A later accepted start rearms failure events for the new charging session while retaining the previous warning. Events and warning deduplication are runtime state; the persistent notification can also be dismissed in HA. A failure warning means the stop is unconfirmed, and does not assert that charging is definitely continuing. Existing ownership rules and command retries still apply.

Tesla observation and standalone Zaptec polling check for continued charging using a sample captured after the last stop request. A charging sample must be fresh (within two minutes), and the stop must have had at least 90 seconds to settle. A missing, stale or future sample does not establish success or failure. The check runs when telemetry arrives, including after a longer polling gap; it is not an independent timer or a guarantee of stopping a charger. Other chargers publish recorded command failures, but do not gain a telemetry watchdog from this change. For an independent safety check, also monitor your charger power sensor and home battery level in HA.

## Send a phone notification on a failed stop

Replace the notify service with your HA Companion App service. `mode: queued` preserves separate vehicle failures arriving together. Add an `entry_id` or `vehicle_id` filter under `event_data` if needed.

```yaml
alias: PowerSync EV stop warning
triggers:
  - trigger: event
    event_type: power_sync_ev_stop_failed
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: EV charging stop needs attention
      message: >-
        PowerSync could not confirm the stop for
        {{ trigger.event.data.vehicle_id }}.
        {{ trigger.event.data.reason or 'Check the car and charger.' }}
        Check actual charging power and the home battery.
mode: queued
max: 10
```

No phone notification is sent automatically by these new events. The persistent HA warning works without this automation; phone delivery requires your notify service and automation to be configured and working.
