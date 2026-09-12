<!-- release: v2.12.1281 -->

## What's Changed

**SolarEdge reconciliation failures now explain the safe block**
When a supervised SolarEdge reconciliation cannot clear its safety latch, PowerSync now records and returns a safe reason such as an unavailable fresh storage readback, an active storage command, an unsupported storage mode, or a retained-baseline mismatch. Battery Integration Details exposes the same result, including the affected logical field for a baseline mismatch.

This does not bypass containment, retry a previous command, or write SolarEdge controls. A reconciliation still becomes ready only after the existing fresh, complete, benign readback confirms the retained baseline.

Update available via HACS
