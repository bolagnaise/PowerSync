<!-- release: v2.12.1738 -->

## SolarEdge battery control

PowerSync can again read battery settings through SolarEdge Modbus Multi 4.0.15. This fixes a readback error that could block supervised recovery and battery export. PowerSync still needs a fresh reading from the selected inverter before it can confirm recovery.

Update through HACS and restart Home Assistant.
