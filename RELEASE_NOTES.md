<!-- release: v2.12.1134 -->

## What's Changed

**Daily Import Cost survives same-day upgrades and reloads**
PowerSync now migrates legacy energy-accumulator coverage when Smart Optimization's independent same-day ledger confirms the exact same imported energy and cost. This prevents a finite Daily Import Cost from becoming `unknown` after updating from a release that predates the new priced-coverage counters. The reconciled coverage is persisted so another reload does not reopen the gap.

**Incomplete pricing remains fail-closed**
Recovery requires matching local date, energy, and monetary totals at persisted precision. Missing prices, non-finite values, even small unpriced energy gaps, and unmatched month-to-date history continue to report partial coverage instead of being presented as an exact cost.

Update available via HACS
