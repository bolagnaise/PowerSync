<!-- release: v2.12.891 -->

# PowerSync v2.12.891

## Fixed

- **CovaU multi-day planning:** Fixed a HiGHS constraint-matrix sizing error when the 48-hour horizon contained more than one daily import or export quota group. The optimiser now keeps daily quota boundaries exact instead of raising `list index out of range` and falling back to the greedy planner.
- **GoodWe Hold SoC:** Added the missing GoodWe EMS Hold SoC route using `conserve` mode. This blocks on-grid battery discharge while allowing excess solar charging, then restores EMS `auto` and the normal inverter operation mode when Hold expires.
- **Hold cleanup safety:** Timed Hold cleanup now runs even when Monitoring Mode is enabled, and a failed GoodWe restore remains visible and persisted so it can be retried instead of being reported as successful.

## Documentation

- Clarified that GoodWe Hold SoC requires EMS entity control and is a no-discharge hold rather than a strict zero-flow battery lock.
