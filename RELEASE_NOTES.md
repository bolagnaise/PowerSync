<!-- release: v2.12.393 -->

## What's Changed

**Make OCPP smart-charging control fail safe**
PowerSync now refuses managed OCPP starts when the HACS OCPP integration or charge point rejects a current-limit update. This prevents dynamic charging from accidentally starting at the charger's unrestricted default current when smart charging is not actually being applied.

**Use real HACS OCPP command results**
OCPP start, stop, and current-limit commands now prefer the HACS OCPP CentralSystem API when it is available, so PowerSync can see real charge point accept/reject responses instead of relying on optimistic Home Assistant switch and number entities.

**Improve HACS OCPP charger detection**
PowerSync now understands HACS OCPP multi-connector entity prefixes and upstream measurand names such as power_active_import, power_offered, energy_active_import_register, and energy_session. Charger status, loadpoint power, and OCPP session tracking should now work across more standard HACS OCPP installs.

**Clarify OCPP setup wording**
The OCPP setup text now describes the HACS OCPP integration path instead of implying that PowerSync runs its own OCPP WebSocket server.

Update available via HACS
