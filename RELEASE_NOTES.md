<!-- release: v2.12.1163 -->

## Two-Tesla charging status, unplug detection, and scheduled-start reliability

This release fixes two related multi-vehicle problems: an unplugged Tesla could remain visible as charging after another Tesla was connected, and Charge By Time / Smart Schedule could repeatedly attempt a start without accepting fresh Wall Connector proof that the intended vehicle had begun drawing power.

### An unplugged vehicle could still appear to be charging

**Update if you use two Tesla BLE prefixes and one prefix contains the other, or if an unplugged car remains shown as connected while its charge-port flap is still open.**

PowerSync combines Fleet/Teslemetry and Tesla BLE observations so each physical vehicle appears once. The BLE supplement used the same fuzzy identity match that is needed for some embedded VIN payloads. For synthetic BLE IDs that was too broad: a short prefix could match inside a longer prefix, causing the longer-prefix vehicle's fresh BLE observation to be skipped. A stale cloud charging row could then survive and double-count EV power.

The charge-port flap was also treated as definitive plug evidence before the explicit BLE charging state. Tesla can leave the flap open after the cable is removed, so a current `Disconnected` state could be overwritten and Smart Schedule could consider the unplugged car ready to start.

**What changed:**

- Synthetic BLE observations are deduplicated by exact normalized identity. The broader embedded-VIN matcher remains unchanged for the payloads that need it.
- Positive measured charge power and an explicit BLE charging-state plug result now outrank flap position.
- Charge-port flap position remains available as a fallback when no definitive charging state or measured draw exists.

### Repeated scheduled starts could queue ahead of the retry cooldown

Smart Schedule evaluates every 30 seconds, while a Tesla physical-start confirmation can wait up to 150 seconds. Multiple evaluations for the same car could enter the start path before the first attempt completed and recorded its retry delay. Those attempts then waited behind the shared EV action lock and could execute later as repeated start and compensating-stop cycles, despite the log reporting a much longer cooldown.

At the same time, the confirmation gate required both a VIN-scoped charging-state transition and measured draw. A Wall Connector could report fresh power for the exact intended VIN while cloud/BLE charging state still said `stopped`, causing a real physical start to be rejected.

**What changed:**

- Only one full Smart Schedule start transaction may be in flight per canonical physical vehicle. A second evaluation for the same car is deferred immediately; a different vehicle remains independently eligible.
- The existing shared action lock and ownership/session safety boundaries are unchanged.
- Fresh Wall Connector power may confirm a start without waiting for a lagging cloud state, but only when it is keyed to the exact 17-character VIN, timestamped after the command, and at or above the established 1.4 kW viable-charging threshold.
- VIN-less or unrelated connector power, stale samples, sub-charging auxiliary draw, and command acceptance alone still fail closed.

The patch includes regressions for overlapping BLE prefixes, disconnected-state versus open-flap precedence, same-vehicle concurrent starts, cross-vehicle independence, delayed cloud telemetry, and exact-VIN connector freshness and power thresholds.
