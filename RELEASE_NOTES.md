<!-- release: v2.12.644 -->

## What's Changed

**Delay solar forecast warnings until the first optimizer run**
PowerSync no longer shows the “No Solar Forecast” warning during the short startup window before the first optimizer run has checked forecast providers. This prevents the dashboard from briefly reporting price-only scheduling immediately after a Home Assistant restart when Solcast or Open-Meteo data is about to load normally.

Update available via HACS
