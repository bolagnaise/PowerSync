<!-- release: v2.12.1066 -->

## What's Changed

**Tesla current updates now fail honestly when the control entity is unavailable**

PowerSync now rechecks the VIN-scoped Tesla charge-current entity after waking
the vehicle and refuses the update if Home Assistant still reports that entity
as missing or unavailable. This prevents a rejected current update from being
logged and stored as though it had been applied, keeps Solar Surplus status
aligned with confirmed runtime state, and allows a later cycle to retry after
the entity recovers. The conservative 5 A limit used while the active charger
capability is unknown is unchanged.

Update available via HACS
