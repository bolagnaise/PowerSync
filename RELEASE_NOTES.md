<!-- release: v2.12.1778 -->

## Local Powerwall telemetry

PowerSync now refreshes solar string voltage, current and power on the two-second local Powerwall cycle when the gateway responds promptly. Open **Powerwalls → Powerwall Telemetry** in the Home Assistant dashboard for the string readings and reported Powerwall 3 component data. The slower string fallback is available when the fast local read is unavailable.

Update through HACS and restart Home Assistant.
