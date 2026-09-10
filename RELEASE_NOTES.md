<!-- release: v2.12.1274 -->

## What's Changed

**Generic Charger configuration changes now reach every control path**
When a Generic Charger switch, current, status, or power entity is updated in PowerSync settings, manual controls and automated charging now use that saved configuration instead of a stale legacy charger record. This prevents an old deleted entity from being selected after an update.

**Missing Generic Charger controls fail safely**
Start and stop actions now require an available `switch.*` control entity. If the configured switch is missing, PowerSync reports a failed command and preserves its tracked charging state rather than reporting a successful stop.

Update available via HACS
