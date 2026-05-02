## What's Changed

**Solar forecast now reality-checks against live production**
On heavily-clouded days the LP optimiser would happily wait for forecast solar that wasn't actually arriving — Solcast might predict 6 kW while you're producing 0.5 kW — and let the battery drain instead of grid-charging. The optimiser now compares current solar power to the next ~90 minutes of forecast; when live is materially below forecast (≤75%) it derates the near-term forecast and fades back to the raw Solcast values over 6 hours, with hysteresis so a single noisy sample doesn't latch you in. Skips dawn/dusk and ≥98% SoC (where curtailment can mask true production). Two new fields in the optimizer status payload — `solar_nowcast_derate` and `solar_nowcast_ratio` — let you see when it's active.

**Solcast forecast slots now align with the optimiser correctly**
The Solcast parser was treating each 30-minute datapoint as a single instant and matching by nearest neighbour, which could shift solar production into the wrong LP slots (an 11:00 datapoint sometimes drove 10:30 charging decisions). Each datapoint is now treated as covering its full 30-minute window from `period_start` to `period_end`, so a 5- or 15-minute LP slot only consumes a datapoint's power if it actually falls within that window. The parser also now recognises alternative Solcast entity IDs (`solcast_forecast_today`, `solcast_pv_forecast_today`) and the `forecasts` / `detailedHourly` / `forecast_tomorrow` attribute fallbacks, so installs that use older/newer integration variants get a forecast instead of silently falling back to zero.

**Battery health card shows where the scan came from**
The Tesla battery-health sensor previously labelled every scan `source: mobile_app_tedapi` regardless of how it was actually collected. The sensor now persists the real source from the scan payload (`ha_local_tedapi`, `ha_fleet_api_relay`, `mobile_app_tedapi`, `mobile_app_cloud_rsa`, etc.) and the HA dashboard's Battery Health card displays a friendly label — "local gateway", "Fleet API relay", "mobile local scan", "mobile cloud RSA" — next to capacity and last-scan date, so you can tell at a glance which path produced the most recent reading.

Update available via HACS
