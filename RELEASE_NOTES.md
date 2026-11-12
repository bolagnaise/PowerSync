<!-- release: v2.12.1688 -->

## AlphaESS battery forecasts

Smart Optimization now reads battery charge and energy telemetry from the AlphaESS connection. Previously it could plan from a default charge level, producing a forecast that disagreed with the measured battery percentage and leaving the charge reading unavailable in AI explanations.

Update through HACS and restart Home Assistant to reconnect the optimizer to the AlphaESS readings. Cloud-only connections remain monitoring only.
