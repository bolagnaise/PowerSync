<!-- release: v2.12.1062 -->

## What's Changed

**AEMO tariff syncs now stop cleanly during reloads**

PowerSync now ties each AEMO settled-price sync to the exact active config-entry
generation. When PowerSync reloads, queued and in-flight dispatch work is
cancelled and joined before teardown, and an older callback cannot write tariff,
Flow Power TWAP/PEA, provider, demand-protection, or Tesla state into the newly
loaded entry.

This prevents the `Task exception was never retrieved` / config-entry `KeyError`
seen when a five-minute AEMO dispatch overlapped a reload. The normal settled-
price delay, startup deferral, provider gates, Monitoring Mode behavior, tariff
uploads, and demand-grid protection remain unchanged for the active entry.

Regression coverage includes callbacks queued before and during unload,
startup-deferred work, completed-task exception harvesting, post-API tariff
writes, Flow Power and Sigenergy state, and Tesla demand-boundary convergence.

Update available via HACS
