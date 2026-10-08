<!-- release: v2.12.1362 -->

## What's Changed

**Include EV demand in AC inverter load following**
When complete EV telemetry is available, PowerSync keeps the EV-excluded home-load value for display while AC-coupled inverter load following uses the gross site load. This prevents the inverter target from being limited to only the house load while an EV is charging. Legacy coordinator/API data continues to use its existing gross load value, and incomplete telemetry still fails closed.

Update available via HACS
