<!-- release: v2.12.1586 -->

## Solar forecast stays visible during fallback planning

The solar and load forecast chart now keeps showing available forecasts when Smart Optimization uses its fallback planner. Previously, the chart could say “Waiting for the next forecast” even while the planner was using valid Solcast data.

This corrects the forecast display; it does not change battery commands or Solcast polling. Update through HACS, restart Home Assistant, and refresh the dashboard after the next optimizer cycle.
