<!-- release: v2.12.1598 -->

## Powerwall recovery in Monitoring Mode

Unfinished Powerwall restores can now finish their tariff cleanup after a restart or reload while Monitoring Mode is on. This prevents a pending restore from repeatedly blocking a change of preferred battery control.

Settings that cannot be confirmed stay pending. Update through HACS and restart Home Assistant, then retry the control preference change after recovery completes.

## Tesla connection after leaving home

Tesla vehicles no longer remain shown as plugged in or charging when an explicitly paired BLE bridge has disconnected and the older Fleet reports have expired. Stale Wall Connector readings cannot restore that status. Unconfirmed EV power remains unavailable in EV and Home readings until new evidence arrives.

Fresh vehicle reports remain supported, including repeated unchanged readings. Update through HACS and restart Home Assistant.
