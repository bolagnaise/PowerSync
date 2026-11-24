<!-- release: v2.12.1818 -->

## Recover a Stop after re-adding a Tesla Bluetooth bridge

Re-adding an ESPHome bridge can leave PowerSync charging controls paused when a previous Stop was unconfirmed. The Home Assistant owner can now verify the original bridge using a local backup and recover after fresh vehicle readings confirm charging is stopped with zero current and power.

Update through HACS and restart Home Assistant. Follow the [recovery guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-command-recovery.md#recover-one-held-stop-after-re-adding-its-esphome-bridge) to use the backup without restoring it.
