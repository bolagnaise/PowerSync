<!-- release: v2.12.1597 -->

## Powerwall recovery in Monitoring Mode

Unfinished Powerwall restores can now finish their tariff cleanup after a restart or reload while Monitoring Mode is on. This prevents a pending restore from repeatedly blocking a change of preferred battery control.

Settings that cannot be confirmed stay pending. Update through HACS and restart Home Assistant, then retry the control preference change after recovery completes.
