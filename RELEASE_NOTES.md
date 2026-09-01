<!-- release: v2.12.1218 -->

## What's Changed

**Custom AGL tariff periods now survive save and reopen exactly**
PowerSync now retains the rows entered in the custom tariff editor rather than
trying to reconstruct them only from runtime TOU ranges. This keeps distinct
weekday and weekend Off-Peak periods visible after reopening settings, and
prevents duplicate weekend rows from replacing weekday periods on a later save.

Existing legacy tariffs are also recognised when their saved Off-Peak ranges
are explicit rather than generated uncovered hours, so the affected tariff can
be corrected without changing optimiser or battery-control behaviour.

Update available via HACS
