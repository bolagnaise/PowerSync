<!-- release: v2.12.1856 -->

## CalibrationRequested no longer pauses Powerwall control on its own

`CalibrationRequested` is only a request. Some Powerwall gateways keep it raised for days before and after a calibration while still following commands, and PowerSync paused battery charge and export control for that whole time unless you ran Refresh Calibration, again after every restart.

PowerSync now pauses for `CalibrationRequested` only once Tesla has actually taken over: the battery keeps discharging for three minutes while PowerSync's force charge or Hold SoC is active. That pause ends when the alert clears, after the battery has drained low and refilled, after 72 hours, or when you run Refresh Calibration. `BatteryCalibration` still always pauses control. This applies to Tesla Powerwalls paired for local control. Update through HACS and restart Home Assistant.
