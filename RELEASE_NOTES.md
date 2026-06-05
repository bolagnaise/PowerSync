<!-- release: v2.12.597 -->

## What's Changed

**Optimizer schedule no longer displays a false drain to 5%**
The optimisation API now keeps the displayed SOC and grid export arrays aligned with the post-processed schedule after Profit Max or Spread Export caps battery export at the configured reserve. This fixes cases where the optimiser had already stopped battery export at the 30% reserve, but the dashboard still showed raw LP export and SOC continuing down to the 5% hardware floor.

Update available via HACS
