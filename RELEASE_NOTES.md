<!-- release: v2.12.1353 -->

## Fixed

- Preserve active user-owned battery controls through Smart Optimization's
  spread-import, spread-export, and short-export-gap post-solve transforms.
- Keep fixed manual discharge power and its projected SOC trajectory out of the
  discretionary export-spreading budget, including AGL/FoxESS export windows
  containing intervening solar-charge slots.
- Prevent the one-slot export bridge from replacing a fixed manual
  self-consumption slot.

## Validation

- Added regressions for both HiGHS and greedy optimizer paths, plus the
  coordinator's post-solve manual-slot and solar-charge-island behavior.
- Published source is validated by the focused and adjacent local test suites;
  installation and live hardware behavior remain unverified.
