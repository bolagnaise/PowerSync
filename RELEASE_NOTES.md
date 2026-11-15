<!-- release: v2.12.1707 -->

## Fewer unnecessary Cost Neutral exports

Cost Neutral now removes automatic battery-export plans below 500 W, including the repeated 0.1 kW intervals that could interrupt normal solar charging. This reduces unnecessary switching between export and self-consumption.

Removed intervals return to self-consumption, with solar charging, battery charge forecasts and daily costs recalculated together. Manual controls and surplus-solar export keep their existing behaviour. A small remainder of the daily cost target may stay uncovered. Update through HACS and restart Home Assistant.
