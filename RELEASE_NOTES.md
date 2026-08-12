<!-- release: v2.12.1078 -->

## What's Changed

**Flat-price battery holds no longer become avoidable discharge-and-recharge cycles**
When the optimizer deliberately preserves battery charge earlier in an equal import/export price window before an actual later grid charge, PowerSync now keeps that plan as Idle through mode projection. This prevents natural self-consumption from being inserted ahead of the planned recharge and avoids the resulting round-trip loss, including on multi-Powerwall sites.

Modest non-parity price spreads, later solar-only charging, reserve behavior, and configurations that intentionally disable Idle keep their existing behavior.

**AC inverter callbacks now stop cleanly across reloads**
Curtailment decisions, controller commands, restore and Powerwall fallback paths, and fast load-following refreshes now verify that they still belong to the active setup generation before reading, commanding, or writing state. Stale callbacks can no longer cache a controller into a removed entry, mutate a replacement runtime, or continue through an exception after reload.

Update available via HACS
