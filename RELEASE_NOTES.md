<!-- release: v2.12.1535 -->

## What's Changed

**Powerwall Tariff discharge and pending restoration**
Tariff force discharge now requests battery export and continues when Tesla accepts the change, without requiring the export setting in readback. Battery export stays enabled afterward. Older sessions stuck restoring the missing-rule fallback can finish once the tariff, mode, reserve and grid-charging settings are restored; other recovery checks still apply.

Update through HACS and restart Home Assistant so retained restoration can retry. An accepted request does not confirm physical discharge; check Battery Power and Grid Power.

Update available via HACS
