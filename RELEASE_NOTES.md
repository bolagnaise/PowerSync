<!-- release: v2.12.1703 -->

## OCPP current-limit recovery

A failed current-limit attempt could prevent later attempts from recovering after transaction control became available. PowerSync now retries after a one-minute cooldown, while still requiring an accepted current limit before starting. Stop commands remain available during the cooldown.

This does not add start-before-limit or persistent default-profile control. Chargers without an available transaction limit still refuse managed starts safely.

## OCPP price forecasts after switching from a generic charger

Old generic-charger profiles are excluded from price forecasts when generic control is disabled and OCPP is selected. This avoids looking up battery level for the inactive generic profile while preserving saved settings.

Update through HACS and restart Home Assistant.
