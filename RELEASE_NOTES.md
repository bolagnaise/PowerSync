<!-- release: v2.12.386 -->

## What's Changed

**Keep FoxESS export active at the optimiser reserve**
FoxESS systems using Flow Power Happy Hour or other export windows now keep the optimiser's export command active when SOC reaches the software reserve. The inverter's own minimum SOC remains the hardware floor, avoiding an early switch to self-consumption that could make the home import during the export period.

Update available via HACS
