<!-- release: v2.12.1737 -->

## Tesla Powerwall scheduled discharge

When a fresh battery reading reduces the safe export time while PowerSync is preparing a tariff-based discharge, the optimizer now uses the shorter duration. This prevents a small battery-level change from unnecessarily cancelling a discharge that can still run within the configured reserve.

Update through HACS and restart Home Assistant.
