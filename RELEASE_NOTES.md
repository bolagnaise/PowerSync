<!-- release: v2.12.1572 -->

## Powerwall settings when commands overlap

An older Powerwall force-charge request now stops retrying its backup reserve after a newer force or restore command takes over. Standalone reserve changes also stop when superseded, and cleanup from a failed charging startup can no longer restore settings over a newer command.

Update through HACS and restart Home Assistant. Settings still require confirmation, and temporary reserve pulses still complete their required restoration. This change does not bypass pending-settings safety checks.
