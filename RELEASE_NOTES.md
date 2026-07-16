<!-- release: v2.12.864 -->

## What's Changed

**Solar Surplus EV settings and manual stops now apply to active sessions**

Active Solar Surplus charging now refreshes its persisted policy on every update, so changed battery thresholds take effect without recreating the session. Manual stops now suppress automatic Solar Surplus restarts, including generic chargers exposed through OCPP under a different loadpoint ID. The existing 10% hysteresis remains: a 90% start threshold pauses an active session below 80%.

**Monitoring-mode reloads no longer restore battery operation**

Reloading PowerSync while monitoring mode is already enabled now stops the optimizer without issuing an executor restore command. A genuine monitoring-mode enable transition still performs its established one-time cleanup.
