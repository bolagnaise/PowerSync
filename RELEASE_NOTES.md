<!-- release: v2.12.1367 -->

## What's Changed

**Enforce Price-Level's home-battery minimum for native Sigenergy telemetry**
Price-Level Charging now reads the active PowerSync battery coordinator when
checking the existing home-battery minimum. Native Sigenergy Modbus Battery
Level data is therefore applied to both opportunity and recovery decisions
even when no separate `sensor.sigenergy_battery_soc` entity exists.

Invalid or unavailable SOC observations remain unknown and preserve the
existing fallback behavior. This fixes charging eligibility; it does not claim
a charger command, hardware acknowledgement, readback, or physical charging
effect.

Update available via HACS
