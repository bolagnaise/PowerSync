<!-- release: v2.12.660 -->

## What's Changed

**Improve Sungrow battery health telemetry**
PowerSync now forwards Sungrow inverter temperature alongside the existing battery temperature in live Modbus telemetry, and uses the configured battery capacity plus BMS state-of-health to populate rated and current capacity values for the mobile Battery Health view. This stops Sungrow systems from showing blank capacity fields when the inverter does not expose a separate rated-capacity register.

Update available via HACS
