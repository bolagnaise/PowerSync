<!-- release: v2.12.1346 -->

## What's Changed

**Preserve profitable export for proven zero-cost battery energy**

The fallback optimizer now keeps solar-origin and measured free-grid battery energy distinct from unknown carry-over energy. When that zero acquisition cost is proven, the planner can export below the concurrent import price when the existing reserve, load, tariff, permission, and site-export-cap checks allow it. Unknown inventory keeps its conservative valuation, and no hardware-control or Monitoring Mode safeguards are changed.

Update available via HACS
