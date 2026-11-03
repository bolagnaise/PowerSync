<!-- release: v2.12.1612 -->

## Restore configured Powerwall tariffs after restart

PowerSync now converts configured Custom TOU tariffs before restoring them to Tesla, including the complete buy and sell schedule. This addresses a restore-payload problem that can leave battery control selection and other Powerwall settings blocked after restart.

Update through HACS and restart Home Assistant, then retry the setting. Controls still wait for Tesla to confirm restoration.
