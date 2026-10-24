<!-- release: v2.12.1495 -->
## What's Changed

### EV home-battery preservation status

PowerSync no longer reports `no_discharge` as applied when the inverter rejects
the battery-preservation command or it times out. It keeps the previous executed
action and retries on a later optimizer execution; an unreachable inverter still
needs its connection restored.

Update through HACS and restart Home Assistant.
