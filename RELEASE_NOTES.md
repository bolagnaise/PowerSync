<!-- release: v2.12.918 -->

## What's Changed

**Sigenergy manual and scheduled exports now use the documented discharge controls**
PowerSync no longer sends the PCS fixed-power register while Sigenergy is in Remote EMS discharge modes 5 or 6. Those modes now use the ESS maximum-discharge register, with current home load included so local consumption does not reduce the intended grid export.

**Safer export limits and failure recovery**
The grid export ceiling remains independent from the battery discharge cap, the ESS request is bounded by both the configured limit and the inverter's rated discharge power, and all affected Modbus registers are captured before dispatch. PowerSync installs the safety limits before enabling discharge and restores the exact previous values if any step fails instead of reporting a partial command as successful.

Update available via HACS
