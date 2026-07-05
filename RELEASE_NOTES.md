<!-- release: v2.12.764 -->

## What's Changed

**Sungrow reloads now close the old Modbus coordinator cleanly**
PowerSync now stops the Sungrow polling coordinator during integration unload/reload and waits for any active Modbus read or control write before disconnecting. This prevents a reload from leaving old Sungrow restore work active while a new coordinator opens another WiNet TCP session.

**Safer WiNet handoff during urgent restore/reload paths**
The Sungrow shutdown path now disables further coordinator polling before disconnecting, reducing repeated `not connected`, `failed connect`, and `no response received` errors after Home Assistant reloads PowerSync.

Update available via HACS
