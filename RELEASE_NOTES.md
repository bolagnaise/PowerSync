<!-- release: v2.12.1502 -->

### Powerwall recovery after a restart

PowerSync now resumes an unfinished Tesla restore even when the old force-mode record has already been cleared. Previously, that restore could remain pending indefinitely and block both optimiser and manual native Max charging after a restart.

Optimisation waits for restoration to be verified. Failed writes or readback keep recovery pending; this update does not bypass site limits, grid-charging permission or control ownership.

Update through HACS and restart Home Assistant.
