<!-- release: v2.12.1675 -->

## Tesla BLE freshness after a device rename

PowerSync now finds Tesla BLE charging-freshness entities even when Home Assistant gave them a different prefix after the ESPHome device was renamed. Fresh charging readings can be recognized again, avoiding charge commands being withheld because the data appeared stale. Companion readings must still belong to the same device and pass the existing value-pairing checks.

When retained Tesla command recovery history holds a bridge, PowerSync now logs a warning pointing to `power_sync.get_ble_recovery_status`, and an information message when the hold releases. This update does not clear an existing recovery hold.

Update PowerSync through HACS and restart Home Assistant.
