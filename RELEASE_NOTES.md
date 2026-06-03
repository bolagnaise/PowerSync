## What's Changed

**Fixed: optimiser reserve collapsing to the hardware floor with Profit Max**
When Profit Max and Auto-Apply Optimizer Reserve were both enabled, the battery could be allowed to fully discharge down to the hardware reserve (e.g. 5%) and stay stuck there. This happened because, on the rare occasions the optimiser couldn't satisfy every constraint, it would briefly re-plan against a temporary 5% floor — and that emergency plan was mistakenly being applied as your real optimiser reserve. The optimiser now ignores reserve recommendations that come from these fallback solves, so your configured reserve is respected and Auto-Apply only adjusts it based on genuine forecasts. This fixes reports of the reserve repeatedly "going back to 5%" and the battery draining further than intended after an export window.

Update available via HACS
