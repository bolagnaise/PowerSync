<!-- release: v2.12.1814 -->

## Fewer startup errors with Network Export off

PowerSync skips the extra optimisation run that changes to an inactive network envelope used to trigger. This avoids a misleading startup error when the optimiser is paused.

Update through HACS and restart Home Assistant.
