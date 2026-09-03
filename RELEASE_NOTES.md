<!-- release: v2.12.1226 -->

## What's Changed

**Flow Power Happy Hour quota continuity**

PowerSync now keeps the active official Flow Power plan's daily quota ledger
when unrelated legacy export-rate or Happy Hour-end settings change. It also
handles the expected midnight reset of daily energy totals, including a brief
stale pre-reset value from the Sigenergy software energy accumulator, without
incorrectly disabling the new day's Happy Hour allowance.

Unexpected daily-counter decreases outside the midnight reset grace period
remain fail-closed, so the export bonus is withheld until safe telemetry is
available.

Update available via HACS
