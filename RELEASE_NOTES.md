<!-- release: v2.12.1722 -->

## SolarEdge solar restoration

PowerSync now checks that a SolarEdge inverter reports its active power limit back at 100% before treating solar restoration as complete. Previously an accepted command could clear the restore state while the inverter still reported a lower limit.

This covers separately configured SolarEdge AC inverters and SolarEdge battery systems using active-power curtailment. Unconfirmed restoration keeps the existing control obligation; uncertain battery-control transactions still require supervised recovery. A confirmed limit does not guarantee solar production when the inverter is sleeping or sunlight is unavailable.

Update through HACS and restart Home Assistant.
