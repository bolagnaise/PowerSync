<!-- release: v2.12.1564 -->

## Powerwall calibration pause

PowerSync now keeps an already-detected calibration pause active when the gateway omits its alert data. Automatic battery charge/export control resumes only after three consecutive valid alert readings confirm the calibration alert has cleared.

This fixes premature clearing of the pause; it does not add a new way to detect calibration when Tesla supplies no alert. Update through HACS and restart Home Assistant.
