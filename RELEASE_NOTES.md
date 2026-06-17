<!-- release: v2.12.665 -->

## What's Changed

**Respect auto-applied export floors after schedule smoothing**

Smart Optimization now carries the auto-applied export reserve floor through the final export-gap smoothing pass. This prevents Profit Max and Spread Export schedules from reintroducing small export bridge slots below the calculated export floor after the solver has already protected that reserve.

Update available via HACS.
