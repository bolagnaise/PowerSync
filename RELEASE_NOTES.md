<!-- release: v2.12.1356 -->

## Fixed

- Preserve an otherwise reachable Charge By Time SOC target when Profit Max
  considers deferring low-value solar export. Discretionary solar-export holds
  now yield to the configured deadline while provider, hardware, manual, and
  grid-charge restrictions remain authoritative.

## Validation

- Added regressions for reachable and genuinely unreachable Charge By Time
  targets, including the reported known-zero solar case.
- Published source is validated by focused and adjacent optimizer tests;
  installation, inverter readback, and live physical charging behavior remain
  unverified.

Update available via HACS
