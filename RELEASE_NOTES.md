<!-- release: v2.12.833 -->

## What's Changed

**Optional PowerSync Cloud energy-flow reporting**
Home Assistant can now opt in to sending grid, solar, battery, and load flow data to PowerSync Cloud every 30 seconds. The reporter supports configurable entity sources, reuses the existing PowerSync Cloud authentication, backs off safely after errors, and stops cleanly when the integration unloads.

**Reserve-aware optimizer scheduling**
Smart Optimization now separates the intentional battery-export floor from the inverter's hardware backup reserve. Natural self-consumption can continue down to the hardware reserve, while intentional grid export still stops at the configured optimizer reserve. Final SOC, grid-flow, and cost forecasts are reconciled after schedule overlays, and priority export is only paired with genuinely reachable economical recharge.

**Sigenergy tariff provider labels**
Sigenergy tariff uploads now preserve the configured provider name for both import and export pricing instead of always labelling tariffs as Amber.

Update available via HACS
