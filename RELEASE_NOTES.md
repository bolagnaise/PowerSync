<!-- release: v2.12.1283 -->

## What's Changed

**Sigenergy export-curtailment status now uses live confirmation**
The DC Solar Curtailment card now requires a fresh Sigenergy zero-export-limit readback and low measured export before it says export is confirmed stopped. This remains accurate after a restart or a manual limit change, while showing whether PowerSync owns the command separately.

Update available via HACS
