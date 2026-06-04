<!-- release: v2.12.581 -->

## What's Changed

**Auto Reserve no longer blocks profitable arbitrage plans**
Auto-Apply Optimizer Reserve now applies only the forecast optimiser reserve recommendation as the plan-wide reserve. The separate home-load export bridge floor remains available to guard export execution, but it no longer raises the whole optimiser reserve and removes charge/export arbitrage windows when Auto Reserve is enabled.

**Optimizer setting changes now refresh the plan immediately**
Changing spread import, spread export, No Idle, battery/grid charge limits, reserve settings, and related Smart Optimization tunables now triggers an immediate re-optimization when the optimiser is enabled. Direct switch changes also refresh the LP plan after the setting changes, so the dashboard and active schedule do not wait for the next polling cycle.

Update available via HACS
