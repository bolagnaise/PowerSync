<!-- release: v2.12.1132 -->

## What's Changed

**Daily export earnings keep counting when EV load attribution is incomplete**
PowerSync now continues integrating independently measured grid import/export energy and tariff costs when a stale or incomplete EV observation makes only the non-EV Home Load calculation uncertain. This prevents Daily Export Earnings from freezing while inverter energy counters and optimizer cost tracking continue to advance.

**Partial accounting is no longer presented as an exact zero**
Sungrow daily hardware totals are now reconciled with the intervals covered by priced samples. If a restart, missing tariff, or earlier telemetry gap means the full day cannot be costed safely, the monetary sensor reports unavailable with coverage and source attributes instead of pairing full-day exported energy with a misleading `$0.00`. Existing interval earnings retain the rate that applied when the energy flowed, including AGL Battery Rewards periods.

**Dual Sungrow sites preserve the grid-facing meter result**
Grid energy, costs, earnings, and coverage now come from the primary grid-facing inverter only, while solar, battery, and load totals continue to aggregate across both inverters.

Update available via HACS
