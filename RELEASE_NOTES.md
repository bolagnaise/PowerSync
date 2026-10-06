<!-- release: v2.12.1354 -->

## Fixed

- Preserve forecast solar charging before a later premium export window when
  stored battery energy has a known zero acquisition cost.
- Keep lower-FIT solar refill separate from grid-import-to-export passthrough,
  while retaining explicit charge blocks, export-priority windows, and future
  self-consumption protection.

## Validation

- Added a regression for the reported flat-import/AGL-price variant, including
  the solar-only charge bound and no-grid-passthrough invariant.
- Published source is validated by focused and adjacent local test suites;
  installation and live hardware behavior remain unverified.

Update available via HACS
