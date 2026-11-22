<!-- release: v2.12.1790 -->

## Tesla Bluetooth charging recovery

PowerSync keeps the Bluetooth connection available when it needs to recover an unconfirmed charging command. A BLE-only Smart Schedule can resume with fresh vehicle readings once recovery is complete.

Update through HACS and restart Home Assistant. If your Bluetooth bridge was already off, check its recovery status before switching it back on. Companion firmware users can find the matching package in the [BLE recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1790/docs/ev-command-recovery.md).
