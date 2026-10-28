<!-- release: v2.12.1526 -->
## Powerwall recovery after native Max discharge

Fixed recovery getting stuck when Grid Export was already set to allow battery export before native Max discharge and its later readback was unavailable. This could prevent both scheduled and manual charging from starting afterward.

PowerSync now leaves that unchanged preference alone. Other settings still require restoration evidence, and recovery still waits for battery power to settle.

Update through HACS and restart Home Assistant so pending recovery can retry. Leave the saved recovery file intact.
