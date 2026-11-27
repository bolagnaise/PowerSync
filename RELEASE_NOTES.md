<!-- release: v2.12.1866 -->

## Powerwall export pauses while Tesla refuses it for calibration

When a Powerwall has a calibration request raised, Tesla can refuse a forced export. During a PowerSync force discharge the battery held its charge, or even charged slowly, while the house imported from the grid, sometimes at peak prices. PowerSync kept the force discharge running and kept planning more exports.

PowerSync now detects this: if a force discharge leaves a charged battery idle while the house imports for three minutes, and Tesla's CalibrationRequested alert is raised, PowerSync returns the battery to normal self-consumption and stops forcing or planning battery export. Charging and normal self-consumption continue, so the calibration can still finish. Export resumes automatically when Tesla clears the calibration request or a full drain-and-refill cycle completes, or straight away if you run **Refresh Calibration**. A notification explains when this happens.

This applies to Tesla Powerwall sites paired for local gateway access. Update through HACS and restart Home Assistant.
