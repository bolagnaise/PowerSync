<!-- release: v2.12.1205 -->

## What's Changed

### Configurable curtailment economics

- Added a curtailment export-earnings threshold under Curtailment settings. The existing behaviour remains the 1c/kWh default; setting it to 0c/kWh curtails only when export earnings are negative.
- Preserved the 0.2c/kWh anti-flap deadband and applied the configured boundary consistently across supported inverter paths, optimizer decisions, forecasts, and status sensors.

### Export-aware Solar Surplus EV charging

- Added a Max Export c/kWh setting to Solar Surplus, defaulting to 15c/kWh.
- Smart Schedule now values solar at the export revenue being given up, so cheaper later grid energy can win over high-value solar export.
- Live Solar Surplus charging rechecks an authoritative export price before starting or changing the EV rate. It pauses above the configured limit and fails closed when no real price is available, while time-critical deadlines can still override the economic preference.
- Manual charging, Boost, and externally owned charging remain outside this automatic price gate.

Update available via HACS.
