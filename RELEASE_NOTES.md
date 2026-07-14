<!-- release: v2.12.841 -->

## What's Changed

**Sigenergy Export Limit stays visible during curtailment**

The Export Limit control now continues to show the persistent site cap while Sigenergy zero-export curtailment temporarily applies a 0 kW hardware limit. Refreshing the mobile Controls screen no longer makes a saved 5 kW cap appear to reset to 0 kW even though Smart Optimization is still correctly constrained.

**Live and configured limits are reported separately**

The Sigenergy settings API now exposes the effective inverter register separately from the configured cap for diagnostics. Systems without a configured cap retain the existing live-register fallback, and this display-only correction does not change hardware commands or optimizer behavior.

Update available via HACS
