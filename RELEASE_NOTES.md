<!-- release: v2.12.1852 -->

## Refresh Calibration now works while CalibrationRequested stays raised

Some Powerwall gateways keep the `CalibrationRequested` alert raised for days before or after a calibration while still following PowerSync's commands. Since v2.12.1849 that alert pauses automatic battery charge and export control, and running **Refresh Calibration** was undone within seconds because the alert was still present.

Refresh Calibration (`power_sync.refresh_calibration`) now keeps control resumed while only `CalibrationRequested` remains raised. The next `CalibrationRequested` after the alert clears pauses control again, and `BatteryCalibration` always pauses control and cannot be overridden. After a Home Assistant restart, run Refresh Calibration again if the request is still raised. This applies to Tesla Powerwalls paired for local control. Update through HACS and restart Home Assistant.
