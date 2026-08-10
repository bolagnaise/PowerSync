<!-- release: v2.12.1058 -->

## What's Changed

**Profit Max can preserve high-value solar export on supported Sigenergy systems**

Profit Max can now defer battery charging during a high feed-in-price interval, export the available solar directly, and replenish the battery from a cheaper reachable solar or guaranteed charge interval later in the plan. This is built into Profit Max rather than exposed as another switch. Provider charge blocks remain separate, and Charge By Time deadlines are protected.

The initial hardware control path is limited to Sigenergy systems with a verified finite export limit. PowerSync applies a persisted zero-charge hold, verifies the live setting, restores normal charge capacity on every transition, disable, and restart, and fails back to self-consumption if any capability or hardware check is uncertain. Existing API and mobile clients continue to receive `self_consumption`, while the integration dashboard uses the additive Solar Export action detail.

The optimizer, lifecycle, API compatibility, dashboard, and Sigenergy Modbus paths are regression-tested. This release was not live-canary tested against physical Sigenergy hardware.

Update available via HACS
