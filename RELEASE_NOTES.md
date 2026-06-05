<!-- release: v2.12.598 -->

## What's Changed

**Flow Power No Idle now smooths post-export SOC**
Flow Power schedules that combine Profit Max, Spread Export, and No Idle now recalculate zero-power self-consumption slots after a capped export window. This prevents the optimiser graph from jumping straight from the configured reserve to the hardware reserve after export, and instead shows the expected gradual home-load discharge path.

Update available via HACS
