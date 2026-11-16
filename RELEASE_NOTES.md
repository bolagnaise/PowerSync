<!-- release: v2.12.1719 -->

## Powerwall fixed battery control

Fixed battery control no longer stops solely because a signed solar meter reports a small standby draw. That draw remains included in site import and export checks.

Update through HACS and restart Home Assistant. Stops caused by stale or unavailable local readings still apply.
