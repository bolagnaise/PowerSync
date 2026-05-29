<!-- release: v2.12.496 -->

## What's Changed

**SolarEdge EV charger power in the energy flow**
PowerSync now detects SolarEdge EV charger power entities such as `sensor.ev_charger_power` and maps them into the shared EV power sensor used by the built-in energy-flow card. SolarEdge systems with a separate SolarEdge EV charger integration can now show active EV charging as EV load instead of folding it into home consumption.

Update available via HACS
