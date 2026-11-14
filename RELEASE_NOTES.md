<!-- release: v2.12.1702 -->

## OCPP charger discovery and EV battery level

An OCPP session-current-limit entity could appear as an extra charger, preventing a single charger's configured EV battery sensor from being used when native battery level was unavailable. PowerSync now keeps that entity attached to the correct charger, including connector-specific entities.

Update through HACS and restart Home Assistant. Existing current-limit and multiple-charger safety checks remain in place; this does not add a station-wide current-limit fallback.
