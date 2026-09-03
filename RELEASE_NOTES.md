<!-- release: v2.12.1230 -->

## What's Changed

**Tesla BLE charging-block automations now recognize fresh measured current**
When a second Tesla BLE bridge has unavailable charger power but a fresh positive
measured charging-current sensor, it is now selected ahead of a disconnected
bridge. Existing `charging_starts` automations keep the exact active BLE target
and can run their configured stop action. Writable requested-current controls,
stale telemetry, and bridge availability alone still do not count as charging.

**Unavailable Tesla BLE power is no longer presented as an idle measurement**
The EV card keeps an unavailable or invalid Tesla BLE power reading distinct
from an explicit 0 kW sample, showing it as unknown instead of `0.00 kW` / Idle.

Update available via HACS
