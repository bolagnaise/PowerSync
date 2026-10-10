<!-- release: v2.12.1371 -->

## What's Changed

**Keep reachable Cost Neutral deadlines executable**

Cost Neutral can now select an eligible grid-charge mode during its initial LP solve when a Charge By Time target is reachable. This prevents the solver's native-solar charging witness from making the deadline infeasible before command-mode projection, while preserving export accounting, reserve floors, grid-charge permissions, and native solar absorption. Solar-aware pre-window headroom remains available for discretionary top-up and is relaxed only when it conflicts with unavoidable native charging.

The regression coverage includes the reported 14-slot five-minute geometry, a minimal one-slot deadline, and a two-slot native-solar/headroom interaction.

Update available via HACS
