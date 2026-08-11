<!-- release: v2.12.1076 -->

## What's Changed

**Fronius solar-export blockers are now explicit**
PowerSync now verifies that the selected Fronius Modbus config entry is loaded before trusting its entities or attempting storage control. Profit Max reports the upstream integration state when solar export cannot be armed, rejects stale entity values after an upstream setup failure, and emits one actionable warning per outage instead of silently leaving the plan in self-consumption.

**Startup reoptimization waits safely for battery telemetry**
Network-envelope reoptimization now treats unavailable startup telemetry as a temporary deferral rather than a failed solve. Active export remains fail-closed, the retry is single-flight, authority is granted only after a successful solve against the current unchanged envelope, and pending retries are cancelled cleanly during unload.

**See raw solar forecasts and proven system curtailment**
The LP Solar & Load graph now shows the raw weather forecast behind planned solar and shades System Curtailment only where explicit optimizer caps or solved LP spillage prove it. Nowcast and weather adjustments remain visually separate and are never mislabeled as curtailment. The same aligned provenance is available through the forecast sensor and optimization API for companion clients while the legacy forecast attributes remain compatible.

**Clearer EV controls on the Home Assistant dashboard**
The EV panel now shows a live countdown when a start or stop delay is active, keeps charging-mode controls in a concise expandable section, and immediately recalculates the dashboard layout when that section changes size.

Update available via HACS
