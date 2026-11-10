<!-- release: v2.12.1673 -->

## Tesla BLE recovery after a bridge restart

BLE recovery could time out without collecting vehicle readings when a sleeping car’s status was still unknown after the bridge restarted. Recovery now uses its bounded wake checks before requesting fresh charging readings, while keeping the hold whenever stopped, zero-current and zero-power proof is missing.

This fix is in the companion bridge firmware. Update both PowerSync references in the existing bridge configuration to `v2.12.1673` and rebuild/install the companion using the [recovery guide](https://github.com/bolagnaise/PowerSync/blob/main/docs/tesla-ble-stop-recovery.md). An HA-only update does not change bridge firmware or clear an existing hold. Preserve pairing and retained history; use the supported recovery action only with the guide’s complete-vehicle and stopped-state prerequisites.
