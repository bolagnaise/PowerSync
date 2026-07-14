<!-- release: v2.12.848 -->

## What's Changed

**CovaU SolarMax tariffs now use measured daily allowances**

Added CovaU as an electricity provider for the current fixture-backed SolarMax plans on Ausgrid, Endeavour Energy, Essential Energy, Energex and SA Power Networks. PowerSync reads and caches the selected public AER/CDR plan snapshot, uses the plan's fixed AEST tariff clock, and shows the effective import and export price as each daily allowance is consumed.

The 50 kWh free-import and 30 kWh premium-export allowances are settled from cumulative connection-point energy meters where available. Missing or discontinuous telemetry fails safely by marking quota confidence unknown and withholding quota-bonus optimization until the next tariff-day reset. Existing GloBird quota behavior now shares the same measured settlement engine without changing its public results.

**SAPN Flexible Exports monitoring is available through certified site equipment**

Added a read-only Network export limits / Flexible Exports setup screen, Home Assistant sensor, dashboard card and versioned API contract. PowerSync can display current, fallback, stale and unavailable operating-envelope states sourced from separately certified inverter or gateway entities, while monitoring mode suppresses intentional PowerSync export actions.

This release is monitoring-only. The tested active export guard remains behind a runtime release gate until the required seven-day SAPN site soak and staged fallback/recovery replay are complete. PowerSync does not implement CSIP-AUS, cannot override a network limit, and is not a replacement for the certified site controller.

**Optimizer and dashboard understand quota and envelope constraints**

Quota-aware schedules prevent simultaneous grid import and export arbitrage, split allowance boundaries correctly and expose planned versus measured quota usage. The optimizer dashboard now shows CovaU quota progress and a step-line network-envelope overlay while preserving a real numeric 0 W limit distinctly from unavailable data.

Update available via HACS
