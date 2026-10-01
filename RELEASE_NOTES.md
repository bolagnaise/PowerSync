<!-- release: v2.12.1341 -->

## What's Changed

**Preserve reachable Charge By Time deadlines from near-full starts**

Charge By Time now keeps the configured SOC floor active through the deadline
even when the battery starts near or above the target. This prevents later
household load from draining a future 04:00 target to a much lower projected
SOC. The deadline hold remains authoritative when Disable Idle is enabled, and
unreachable targets are still capped at the physically reachable SOC.

Update available via HACS.
