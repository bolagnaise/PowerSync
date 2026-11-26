<!-- release: v2.12.1858 -->

## Powerwall calibration "resting" phase is now detected

During a Tesla calibration the Powerwall can hold the battery idle at its low point while the Tesla app shows *Powerwall resting to complete calibration*. Only `CalibrationRequested` is raised then, so PowerSync did not recognise the calibration, and a planned force charge in that phase would silently do nothing.

PowerSync now also treats `CalibrationRequested` as an active calibration when a force charge is running with grid charging allowed but the battery stays idle (charging under 0.5 kW, below 95%) for three minutes. Calibration Active then turns on and automatic battery control pauses until the calibration ends. Deliberately small charge targets and Hold SoC are not affected. Update through HACS and restart Home Assistant.
