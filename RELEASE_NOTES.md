<!-- release: v2.12.1369 -->

## What's Changed

### Persistent battery provenance

- Battery energy provenance now survives both the midnight daily-cost reset and Home Assistant reloads. PowerSync retains measured solar-origin energy, grid-origin energy, and the actual acquisition cost of the remaining grid-origin portion.
- Battery discharge now reduces solar, grid, and unknown inventory proportionally, including the retained grid acquisition cost.

### Safer reconciliation

- Restored inventory is reconciled against live battery SOC and capacity. Telemetry gaps, capacity changes, and unexplained energy are represented as unknown rather than being misclassified as solar or grid energy.
- Legacy or invalid stored provenance keeps the existing median-reference valuation fallback until new measurements establish a durable inventory.

Update available via HACS
