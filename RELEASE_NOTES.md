<!-- release: v2.12.430 -->

## What's Changed

**Support separate Sungrow SG solar-only inverters**
Sungrow battery installs can now enable the AC inverter path for a separate SG-series PV inverter, such as an SG10RT alongside an SH15T battery inverter. PowerSync will poll the extra inverter independently instead of only showing the main Sungrow battery coordinator.

**Include SG inverter output in site solar**
The main solar power sensor now adds the separately polled Sungrow AC inverter output to the SH inverter solar value and exposes attributes showing the battery-inverter, AC-inverter, and combined totals. This lets mixed SH + SG sites see total site solar rather than only the hybrid inverter side.

Update available via HACS
