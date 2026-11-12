<!-- release: v2.12.1685 -->

## OCPP charging automations

Automations that react to OCPP charger status now use only entities from the Home Assistant OCPP integration. Tesla BLE connection sensors, inverter and tariff status sensors, and unrelated energy sensors can no longer be mistaken for OCPP charger readings.

Update through HACS and restart Home Assistant. This does not clear an existing Tesla BLE recovery hold.
