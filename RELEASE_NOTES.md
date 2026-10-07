<!-- release: v2.12.1358 -->

## Fixed

- Correct FoxESS optimizer export commands for grid-meter remote-discharge
  control. The command now uses the final planned whole-site grid export,
  including the solved solar and household-load contribution, so active PV no
  longer displaces the planned battery-to-grid export.
- Preserve the existing battery, site-export, reserve, Monitoring Mode, manual
  ownership, acknowledgement and restore gates. Batteries without this FoxESS
  grid-meter contract keep their existing target semantics.

## Validation

- Added sunny-site and no-solar regression coverage for the reported 615 W
  battery-export versus 1.792 kW whole-site-grid-export variant.
- Focused and adjacent optimizer/control tests passed; a broader legacy fixture
  batch retains unrelated failures in existing API/stub setup tests.
- Installation, inverter readback after this release, and live physical export
  remain unverified.

Update available via HACS
