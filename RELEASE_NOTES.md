<!-- release: v2.12.1076 -->

## What's Changed

**Fronius solar-export blockers are now explicit**
PowerSync now verifies that the selected Fronius Modbus config entry is loaded before trusting its entities or attempting storage control. Profit Max reports the upstream integration state when solar export cannot be armed, rejects stale entity values after an upstream setup failure, and emits one actionable warning per outage instead of silently leaving the plan in self-consumption.

**Startup reoptimization waits safely for battery telemetry**
Network-envelope reoptimization now treats unavailable startup telemetry as a temporary deferral rather than a failed solve. Active export remains fail-closed, the retry is single-flight, authority is granted only after a successful solve against the current unchanged envelope, and pending retries are cancelled cleanly during unload.

Update available via HACS
