<!-- release: v2.12.542 -->

## What's Changed

**Profit Max auto-reserve can lower when the forecast allows it**
Auto-apply optimizer reserve now keeps its intended Profit Max behavior: it can lower the live software reserve when the forecast says home load is covered through the next charging opportunity. This corrects the previous release, which treated the saved manual Minimum discharge level as a hard Profit Max export floor.

**Forced export still respects the live optimizer floor**
Profit Max force-export now uses the live forecast-tracked optimizer reserve as the boundary. It can export down to that selected floor, but it still blocks or cancels force-export if the current or projected SOC would cross below it.

Update available via HACS
