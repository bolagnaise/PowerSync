<!-- release: v2.12.1339 -->

## What's Changed

**Reconcile external Generic Charger sessions in Solar Surplus**

Solar Surplus now detects a fresh, configured Generic Charger power reading even
when PowerSync has not started the session. Under the default override policy,
the session is adopted for scoped rate control and stopped when the battery floor
or export-price policy disallows charging. Under `yield`, the session remains
external and PowerSync does not send a charger command.

Stop requests remain explicitly unconfirmed until a fresh zero-power readback is
observed, with failed or indeterminate requests retained for retry. Stale or
missing power readings are not treated as active charging, and Generic Charger
control now requires a configured switch or amps entity.

Update available via HACS.
