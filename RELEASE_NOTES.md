<!-- release: v2.12.826 -->

## What's Changed

**Correct generic EV power on the dashboard**
The built-in energy-flow card now removes a generic charger's measured draw from the Home branch before rendering the EV as its own branch. This prevents generic EV charging from appearing twice while leaving Tesla, Teslemetry, V2G, and manually configured cards unchanged.

Update available via HACS
