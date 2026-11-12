<!-- release: v2.12.1681 -->
## Confirm unchanged Tesla BLE current settings

Pedro Tesla BLE bridges can now confirm a current setting that already matches the requested value, even when Home Assistant suppresses an identical Charging amps update. Confirmation uses a fresh vehicle-reported current-limit sample and keeps the existing identity and safety checks.

Update through HACS and restart Home Assistant, then rebuild your existing Pedro bridge YAML with the **v2.12.1681** freshness or stop-recovery package references in the [BLE recovery guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/tesla-ble-stop-recovery.md). Preserve the bridge identity, keys and pairing. An HA-only update does not add the sample or clear an existing hold; retained commands still require the supported recovery action and its stopped-vehicle and complete-inventory checks. This change does not apply to yoziru firmware.
