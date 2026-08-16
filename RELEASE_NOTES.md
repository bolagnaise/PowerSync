<!-- release: v2.12.1122 -->

## What's Changed

**Tesla Home Load now follows the observation that actually changed EV power**
PowerSync now tracks Tesla charging power, charging state, and connection timestamps independently. Newer home-location or cable metadata can no longer make older vehicle power appear current and hide a real Wall Connector stop or restart.

**Duplicate and unidentified Wall Connectors retain physical identity**
Same-VIN Fleet, Teslemetry, and BLE observations now merge by source time instead of maximum power, so an older nonzero reading cannot revive after a newer stop or suppress a newer restart. Multiple Wall Connectors without VINs remain separate physical loadpoints, and an explicit VIN is never reassigned heuristically to another vehicle.

**Telemetry safety boundaries remain unchanged**
Away vehicles stay excluded, distinct or unmeasured chargers continue to fail closed, auxiliary charging load remains single-counted, and the correction does not issue or alter any vehicle, charger, battery, or optimizer command.

Update available via HACS
