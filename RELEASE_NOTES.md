<!-- release: v2.12.1084 -->

## What's Changed

**Use live FoxESS H3 battery voltage for current-based power limits**
The FoxESS entity bridge now recognizes the H3 Smart `batvolt_1` and `invbatvolt_1` sensors and uses a valid live pack voltage when converting BMS current limits to power. This removes the incorrect 500 V assumption behind 25 kW maximum charge and discharge readings, keeps manual optimiser limits authoritative, and aligns automatic planning limits with live telemetry.

**Include FoxESS CT2 AC-coupled solar in daily energy**
Positive CT2 generation from the selected `foxess_modbus` entry now contributes to live and daily solar totals. CT2 energy is accumulated separately across restarts and combined with the inverter's DC daily counter without binding unrelated similarly named entities.

Update available via HACS
