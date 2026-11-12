<!-- release: v2.12.1683 -->

## Tesla BLE current confirmation

PowerSync can now confirm a charging-current command when Home Assistant has added a device-name prefix to the companion current-limit sample. This fixes commands remaining unconfirmed even when the vehicle reports the requested current.

Update through HACS and restart Home Assistant. Keep existing entity names; this change does not require another firmware rebuild. Existing held command history still requires the supported recovery action.
