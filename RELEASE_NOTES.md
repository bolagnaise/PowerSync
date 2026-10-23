<!-- release: v2.12.1462 -->

## What's Changed

**Clear optimiser access status**
The 24-Hour Optimizer Plan card now shows when HA optimiser access has expired, Smart Optimization is disabled in settings, recovery is pending, or a manual Resume is required. It hides old forecasts while automatic optimisation is stopped. The settings screen also distinguishes the saved On switch from the live optimiser state.

**Battery charge and export limits**
The maximum charge setting now explains that solar and grid power share the inverter's total battery charging limit. Sigenergy export commands now keep the whole-site PCC target within the configured grid export cap, while retaining a separate battery discharge target for house loads.

**Fresher Powerwall tariff updates**
Periodic price syncs no longer retry an old tariff after a long server delay and interfere with the next fresh update. Manual, startup, and restore syncs retain their existing retry behaviour.

Update available via HACS
