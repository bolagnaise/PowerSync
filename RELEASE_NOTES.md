<!-- release: v2.12.776 -->

## What's Changed

- Fixed Sungrow SH / WiNet-S night-time PV telemetry filtering so the SH10RS PV register alias is detected during normal low-power self-consumption as well as forced-discharge export windows.
- When the PV register mirrors battery discharge after sunset, PowerSync now reports solar as `0 kW` and derives home load from battery discharge plus grid power, preventing inflated dashboard/home-load values like battery discharge being counted twice.
- Added regression coverage for the low-power self-consumption case reported on SH10RS where PV and battery discharge matched exactly with grid power near zero.

Update available via HACS.
