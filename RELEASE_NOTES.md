<!-- release: v2.12.554 -->

## What's Changed

**Disabling curtailment now restores inverter power limits**
PowerSync now immediately restores export and power limits it previously applied when the main solar curtailment toggle is turned off. This covers Sigenergy zero-export limits and other tracked inverter curtailment paths, so disabling curtailment no longer leaves a PowerSync-owned limit stuck on the inverter.

**Curtailment state is cleared only after restore succeeds**
When PowerSync restores a curtailed inverter during option changes, it now clears the cached curtailment state and reapply markers only after the hardware restore call succeeds. This keeps the runtime state aligned with the real inverter state if a restore command fails.

Update available via HACS
