<!-- release: v2.12.1870 -->

## Bluetooth Tesla charging is no longer stuck by the bridge's current limit

A Tesla BLE bridge has its own maximum charging current, set in its firmware. When it was lower than the current configured in PowerSync, for example a bridge built with 15 A on a 32 A charger, Smart Schedule's start failed with "could not pre-set the safe initial rate … the rate command's outcome is unconfirmed" and kept retrying, and Boost could wait on the same unconfirmed command.

PowerSync now recognises that the car is already at the bridge's maximum, counts it as applied and starts charging. It also logs a warning when the bridge's maximum is below your configured current. Every BLE charging mode, including Boost and Solar Surplus, is limited to that maximum. To charge at the full current, set `charging_amps_max` (for example `"32"`) in the bridge's ESPHome YAML and reinstall its firmware.

## Bridge firmware: the car's own charging limit

The PowerSync Tesla BLE companion firmware now reports the highest current the car accepts from the connected charger, the same maximum the Tesla app shows, as **Charge current request max**. PowerSync uses it as the Bluetooth charger limit. The bridge's older "Max amps" sensor reported the current setting rather than the limit.

To use it, rebuild your bridge with the companion pinned to v2.12.1870 and `charging_amps_max: "48"`, then reinstall it from ESPHome. Keep your existing device name, keys and pairing. The steps are in [Install the companion component](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-command-recovery.md#install-the-companion-component). An existing bridge keeps working without the update, but it stays limited to the `charging_amps_max` it was built with.

Update through HACS and restart Home Assistant.
