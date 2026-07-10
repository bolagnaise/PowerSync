<!-- release: v2.12.818 -->

## What's Changed

**OCPP dashboard now distinguishes offered capacity from active charging power**
Fixed the EV Charging dashboard incorrectly showing an unplugged OCPP charger as charging at its advertised maximum power. PowerSync now uses only delivered-power measurements such as Active Power Import or Current Power to determine charging state, while still recognizing Power Offered for charger discovery. Available chargers with no vehicle connected now remain idle instead of displaying a false charging badge, power draw, and grid source.

Update available via HACS
