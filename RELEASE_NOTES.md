<!-- release: v2.12.1072 -->

## What's Changed

**Tessie manual restart protection now follows the vehicle VIN**

PowerSync now resolves Tessie display-name charging sensors through their
VIN-associated vehicle device before applying Solar Surplus manual-restart
protection. If a driver restarts charging after Solar Surplus stopped the
vehicle, automated rate control stays suspended until that manual charge ends,
even if charging-state or power telemetry is briefly unavailable.

When duplicate Tesla providers expose the same VIN, an actively charging state
is preferred over an idle or stale provider. Existing minimum-current safety is
unchanged: cloud capability is used only on a verified cloud command path,
while BLE and unknown paths retain the conservative 5 A floor.

Update available via HACS
