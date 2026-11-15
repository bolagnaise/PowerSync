<!-- release: v2.12.1705 -->

## Generic charger detection and price-level control

PowerSync no longer mistakes a power monitor with a matching charger switch for Tesla BLE solely from those entity names.

Price-level battery-target control now treats generic chargers without an amps control as on/off devices. When the available budget cannot support the configured full charge rate, charging pauses instead of reporting reduced amp settings that the switch cannot apply. Generic chargers with an amps control keep variable-rate operation; kW-only rate controls remain unsupported.

Update through HACS and restart Home Assistant. Check the configured grid-import limit and battery-charge reservation if charging pauses unexpectedly.
