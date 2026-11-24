<!-- release: v2.12.1818 -->

## Recover a Stop after re-adding a Tesla Bluetooth bridge

Re-adding an ESPHome bridge can leave PowerSync charging controls paused when a previous Stop was unconfirmed. The Home Assistant owner can now verify the original bridge using a local backup and recover after fresh vehicle readings confirm charging is stopped with zero current and power.

Update through HACS and restart Home Assistant. Follow the [recovery guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/ev-command-recovery.md#recover-one-held-stop-after-re-adding-its-esphome-bridge) to use the backup without restoring it.

## Tesla Fleet Boost after a site safety refusal

If a site safety check blocks Boost before Tesla Fleet receives Start, PowerSync shows the reason without keeping a charging recovery hold. An older expired Boost can clear while Solar Surplus is waiting when a fresh report for the same car confirms it is stopped at zero current and power.
