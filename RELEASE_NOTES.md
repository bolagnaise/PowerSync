<!-- release: v2.12.443 -->

## What's Changed

**Sungrow AC-coupled solar now contributes to home load**
Sungrow SH systems with a separately configured AC inverter now include that inverter output when PowerSync reconciles solar, grid, and battery power into home load. This prevents AC-coupled solar from disappearing from the load calculation when the inverter reports zero native load, and exposes the AC inverter contribution as `ac_inverter_solar_power` for dashboards and diagnostics.

Update available via HACS
