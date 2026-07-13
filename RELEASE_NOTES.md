<!-- release: v2.12.832 -->

## What's Changed

**Preserve home load in spread-export forecasts**
The optimizer's spread-export post-processing now keeps battery power used by the home separate from power sent to the grid. Variable home demand, reserve-floor fallbacks, inverter discharge limits, and short bridged gaps now retain the home contribution and account for total battery discharge in the SOC forecast instead of showing `Powering Home` as zero.

Update available via HACS
