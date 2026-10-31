<!-- release: v2.12.1579 -->

## Keep battery energy costs through restarts

PowerSync now preserves saved battery energy sources and costs while waiting for the first measured battery level after a restart or integration reload. This prevents a temporary startup estimate from turning known solar or grid energy into unknown energy and unnecessarily restricting exports.

Pending battery accounting is also saved when shutdown battery restoration fails, while recovery remains pending. Update through HACS and restart Home Assistant. Energy already recorded as unknown keeps its conservative valuation; the update cannot reconstruct its original source.
