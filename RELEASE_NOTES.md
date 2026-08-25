<!-- release: v2.12.1195 -->

## What's Changed

**GoodWe systems now run a separately configured AC inverter through the
automatic curtailment path.**

When both the GoodWe battery-curtailment and AC-inverter-curtailment options
are enabled, PowerSync now evaluates the GoodWe export-limit controller and a
separate AC inverter controller independently for both periodic and Amber
event-driven checks. An unavailable GoodWe direct-controller surface no longer
prevents the AC inverter from receiving its own curtail or restore decision.
The existing same-hybrid guard remains in place, so PowerSync does not issue a
duplicate command to the same device.

**Enphase status counters now stay internally coherent.**

PowerSync continues to expose the Envoy EIM's site production power, but keeps
daily and lifetime production counters from the same inverter measurement row.
The result no longer combines an EIM daily value with an inverter lifetime
value, and ignores malformed or impossible counter pairs rather than publishing
an invalid daily total.

**Saving a legacy GoodWe direct profile now preserves an explicit EMS entity
relay.**

Existing GoodWe entries that use direct telemetry together with a configured
Home Assistant EMS entity prefix retain that command route when the connection
profile page is saved. Selecting an explicit entity-telemetry profile remains
unchanged.

This release does not add AC-coupled solar aggregation or alter external
Enphase polling behavior; those require a separate topology and ownership
contract.

Update available via HACS
