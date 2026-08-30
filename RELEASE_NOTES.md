<!-- release: v2.12.1209 -->

## What's Changed

**Tesla BLE now shows measured charging current separately from the requested target**
PowerSync now reads both legacy and current Tesla BLE measured-current sensors in its EV loadpoint status. When the bridge reports a different actual current after a requested-rate command, the EV view keeps the requested target and command power separate from the observed current and power instead of presenting the accepted setpoint as physical convergence. This improves diagnosis of bridge or vehicle-side rate changes without adding new charging commands.

Update available via HACS
