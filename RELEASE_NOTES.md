<!-- release: v2.12.1629 -->

## More accurate Tesla BLE telemetry status

Fresh BLE charging-state and battery-level reports now update the EV card's telemetry status even when their values stay the same or Fleet data is older. Charging power still needs its own fresh measurement, so a recent battery percentage cannot make old watts appear current.

Expired or invalid BLE receipt timestamps no longer let a republished charging-state value refresh the telemetry status. BLE connectivity alone still does not establish fresh vehicle data. Bridges without raw receipt entities retain their existing HA timestamp fallback; this update does not repair stalled bridge polling or add receipt support to bridge firmware.

Update through HACS and restart Home Assistant.
