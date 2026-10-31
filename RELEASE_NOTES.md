<!-- release: v2.12.1572 -->

## Powerwall settings in automations

Automations now wait for a Grid Export Rule change to finish before moving to the next action. Previously, a following Grid Charging action could overlap the export settings change and fail verification.

An older Powerwall force-charge request now stops retrying its backup reserve after a newer force or restore command takes over. Standalone reserve changes also stop when superseded, and cleanup from a failed charging startup can no longer restore settings over a newer command.

Update through HACS and restart Home Assistant. Settings still require confirmation, and temporary reserve pulses still complete their required restoration. This change does not bypass pending-settings safety checks.
