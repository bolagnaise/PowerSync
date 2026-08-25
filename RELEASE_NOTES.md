<!-- release: v2.12.1196 -->

## What's Changed

**Tesla Smart Schedule now recovers when an active deadline charge stops
unexpectedly.**

PowerSync now reconciles its remembered charge-current command with fresh
Tesla telemetry during an owned Smart Schedule session. If the vehicle reports
`stopped` and the measured current is zero while the plan still requires
charging, PowerSync clears the stale commanded-current state and uses the
existing vehicle-scoped start path to resume the session. This prevents a
remembered 32 A command from masking a physically idle car until the departure
deadline passes.

The recovery is scoped to the exact Tesla loadpoint, so it works from the
vehicle telemetry used with a UMC and keeps simultaneous Wall Connector
sessions independent in multi-vehicle homes. Retries are limited to once every
five minutes after a failed restart, and unowned or externally controlled
sessions are not taken over.

Update available via HACS
