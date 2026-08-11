<!-- release: v2.12.1071 -->

## What's Changed

**Tesla Solar Surplus now respects low-current capability and manual restarts**

For Tesla vehicles controlled through a VIN-scoped cloud charge-current
entity, PowerSync now uses the entity's proven positive minimum during the
Solar Surplus stop delay. Supported vehicle and charger combinations can
therefore reduce below the conventional 5 A floor, including to 1 A, while BLE
and unknown control paths retain the conservative 5 A minimum. The configured
Solar Surplus start threshold remains unchanged.

PowerSync also distinguishes a fresh external charging transition from stale
post-stop telemetry. If a driver restarts charging after Solar Surplus stopped
the vehicle, automated rate control is suspended for that manual charge instead
of immediately stopping it again, then resumes after the manual charge ends.

Update available via HACS
