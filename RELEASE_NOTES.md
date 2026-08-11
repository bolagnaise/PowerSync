<!-- release: v2.12.1063 -->

## What's Changed

**Paired Tesla BLE vehicles now stay on the local control path**

PowerSync now reads Tesla EV provider and BLE-prefix settings from the complete
effective config entry, including legacy values that still live in config data.
In Fleet/Teslemetry + BLE `Both` mode, an explicitly paired VIN therefore keeps
using its own ESPHome Tesla BLE bridge for start, stop, charge-limit, and
charging-current commands instead of silently defaulting to cloud control.

A sleeping vehicle whose ESPHome charger entity is temporarily `unknown` still
uses BLE so the bridge can wake it first. If Home Assistant explicitly reports
that paired BLE control entity as `unavailable`, PowerSync skips the unreachable
local command and falls through to the matching Teslemetry Bluetooth vehicle,
then the matching Fleet vehicle. Fallback remains VIN-scoped and never depends
on device-registry order.

Fleet/Teslemetry continues to provide the vehicle name and telemetry identity,
while BLE remains the preferred command path. Ambiguous multi-vehicle setups
still fail closed until each VIN is explicitly paired to a BLE prefix; BLE-only
and Fleet-only configurations are unchanged.

Regression coverage verifies legacy config compatibility, sleeping versus
unavailable BLE entities, BLE-first routing, and vehicle-matched Teslemetry and
Fleet fallbacks. The focused Tesla EV suite passes on Python 3.12.

Update available via HACS
