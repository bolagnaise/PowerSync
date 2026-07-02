<!-- release: v2.12.748 -->

## What's Changed

**Keep PowerSync loaded when Tesla live status is empty**
PowerSync now warms the paired Powerwall local coordinator before the first Tesla cloud refresh, then uses that local LAN snapshot for energy telemetry if Tesla returns an empty `live_status` response. This prevents Home Assistant restarts from leaving the PowerSync config entry stuck in retry/backoff during a Tesla cloud reporting outage when the Powerwall gateway is still reachable locally.

Update available via HACS
