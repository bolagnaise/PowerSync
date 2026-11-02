<!-- release: v2.12.1604 -->

## Powerwall local charging during recovery

Local V1R charging now keeps grid-charging setting changes paused when a previous Powerwall settings request is unresolved. This prevents charging preparation or restoration from bypassing the existing recovery warning.

Timed power controls that need no settings change retain their existing safety checks. This update does not clear an unresolved Repair or reopen local settings access. Update through HACS and restart Home Assistant.
