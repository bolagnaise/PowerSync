<!-- release: v2.12.1512 -->

## Fixed

### Powerwall export after Idle

Fixed native Max export failing to start after the optimiser leaves Idle or restores the backup reserve. PowerSync now keeps Tesla’s recently reported battery power rating while refreshing changed site settings, so the refresh no longer makes a known rating temporarily unavailable.

Unknown or expired ratings still block startup, and reserve, export-limit and ownership checks remain in place. Update through HACS and restart Home Assistant, then check the next intended export window.
