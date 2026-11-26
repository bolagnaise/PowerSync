<!-- release: v2.12.1849 -->

## Powerwall calibration is now detected when Tesla reports CalibrationRequested

Some Powerwall gateways start a battery calibration with a `CalibrationRequested` alert and never raise `BatteryCalibration`. During that cycle the gateway drains the battery and ignores charge commands, but PowerSync kept showing **Calibration Active** as off and continued its planned force charge and export windows against it.

PowerSync now treats `CalibrationRequested` the same as `BatteryCalibration`: **Calibration Active** turns on and automatic battery charge and export control pauses until three consecutive alert readings no longer contain either alert. This applies to Tesla Powerwalls paired for local control. Update through HACS and restart Home Assistant.
