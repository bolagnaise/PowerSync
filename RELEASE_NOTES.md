<!-- release: v2.12.380 -->

## What's Changed

**Quieter OCPP control for chargers that reject current-limit profiles**
PowerSync now remembers when an OCPP charge point rejects current-limit updates during a dynamic charging session and stops retrying the same unsupported profile command every update cycle. Charging can still start and stop through the configured OCPP switch, but users should no longer see repeated `Set current limit rejected by CP` warnings caused by PowerSync retrying a capability the charger does not accept.

**Avoid duplicate OCPP start commands while already charging**
PowerSync now skips duplicate OCPP start calls when the charger switch is already on and the connector is not in `Finishing`. It still performs the off/on reset when the connector is in `Finishing`, preserving the recovery behavior added for chargers that need a fresh RemoteStart after stopping.

Update available via HACS
