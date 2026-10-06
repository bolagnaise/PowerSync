<!-- release: v2.12.1355 -->

## Fixed

- Route automatic solar curtailment for installations with only a separately
  configured AC inverter enabled, so negative export pricing reaches the
  existing AC-inverter curtail and restore path.
- Keep AC-only eligibility independent from battery export and native-DC
  permissions, preserving monitoring-mode, EV-ownership, duplicate-endpoint,
  and battery-absorption safety gates.

## Validation

- Added REST and WebSocket regressions for AC-only negative-price curtailment
  and economic-price restoration, plus permission-isolation coverage.
- Published source is validated by focused and adjacent local test suites;
  installation, inverter readback, and live physical export behavior remain
  unverified.

Update available via HACS
