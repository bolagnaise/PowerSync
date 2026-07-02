<!-- release: v2.12.747 -->

## What's Changed

**Fix SolarEdge battery power entity detection**
PowerSync now prefers SolarEdge battery-specific power entities such as `sensor.solaredge_battery1_power` before falling back to the generic inverter `sensor.solaredge_dc_power`. This prevents the SolarEdge entity bridge from mistaking inverter DC solar input for battery power, which could make the power-flow card show incorrect battery and home-load values.

Update available via HACS
