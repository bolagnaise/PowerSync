<!-- release: v2.12.1228 -->

## What's Changed

**Correct Sigenergy Local Modbus monitoring energy flow**

PowerSync now converts the Sigenergy Local Modbus integration's native battery
power sign before using it for Home Load reconstruction and the Energy Flow
card. Discharge is therefore displayed as battery supply rather than grid
charging, and a valid battery-plus-grid load is no longer clamped to 0 kW.

This applies only to the monitoring-only Sigenergy Home Assistant profile. The
direct PowerSync Modbus path already performed this conversion and is unchanged;
the release does not add hardware control or alter inverter operating modes.

Update available via HACS
