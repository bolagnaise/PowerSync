<!-- release: v2.12.1345 -->

## What's Changed

**Bound Tesla demand-period grid-charging restore retries**
PowerSync now preserves Tesla's accepted-but-unobservable `site_info` result instead of treating it as a confirmed hardware state or retrying the same enable indefinitely. Outside-peak restore attempts are shared across the minute and TOU enforcement paths, capped per demand boundary, and retried on the next boundary; a later direct read-only confirmation can resolve the pending state.

This change does not infer physical charging from an accepted API response, a missing readback field, or the Home Assistant switch state. Monitoring Mode and peak-period disable enforcement remain unchanged.

Update available via HACS
