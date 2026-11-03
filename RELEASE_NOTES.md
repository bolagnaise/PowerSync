<!-- release: v2.12.1611 -->

## Restore saved Powerwall tariffs when switching control methods

PowerSync now expands abbreviated time fields in saved Tesla tariffs before restoring them. This fixes a restore-payload problem that can leave switching between Local V1R and Automatic battery control blocked, while preserving the saved prices and schedule.

Control switching still waits for successful restoration and confirmation from Tesla. Update through HACS and restart Home Assistant, then retry the control-method change.
