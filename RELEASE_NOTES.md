<!-- release: v2.12.583 -->

## What's Changed

**Spread export now respects the Auto Reserve floor**
Smart Optimization now keeps the spread-export post-processing step above the Auto Reserve home-load bridge floor. This prevents a planned export window from being extended back down to the hardware reserve after the optimiser has already calculated a higher floor for covering post-export home load.

**Flow Power Profit Max plans stay aligned with the displayed reserve**
Profit Max export windows can still spread available export energy across the allowed high-value window, but the spreader no longer creates below-floor export actions. The 24-hour plan should now stop export at the same reserve floor shown by Auto Reserve instead of displaying a drain to 5%.

Update available via HACS
