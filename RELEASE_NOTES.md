<!-- release: v2.12.1617 -->

## Powerwall solar and export-limit forecasts

Smart Optimization now includes the export limit reported by a paired Powerwall gateway when it is lower than the optimizer's configured limit. This prevents plans from predicting battery depletion and extra charging later when surplus solar already fills the site's export allowance.

Update through HACS and restart Home Assistant. Gateway discharge protections still apply.
