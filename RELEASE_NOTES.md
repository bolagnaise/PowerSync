<!-- release: v2.12.810 -->

## What's Changed

**Optimizer plan separates home load from export**
The 24-hour optimizer plan now splits battery discharge during export windows into the portion sent to the grid and the portion still powering the home. This keeps the chart and schedule payload from showing home consumption as zero during export periods when the battery is actually covering house load as well as exporting.

Update available via HACS
