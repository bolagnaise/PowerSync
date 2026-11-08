<!-- release: v2.12.1652 -->

## More reliable Tesla BLE charging readback

Tesla BLE charging commands now allow a bounded 35-second readback window and can request up to three exact-bridge data refreshes. Fresh vehicle evidence still decides confirmation; the charging command is sent once, and ownership changes or expired requests stop the wait.

New optional PedroKTFC and yoziru firmware packages keep steady charging power fresh using actual incoming samples and pair each raw value with its receipt time. The Pedro Stop-recovery package includes freshness automatically. Follow the [BLE setup guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1652/docs/tesla-ble-reliability.md) for the correct firmware family and pinned versions. Both families were compile checked for ESP32 and ESP32-S3; the bridge firmware must be rebuilt to add these entities.

The new Inspect BLE recovery status action reports freshness capabilities and retained command requirements without sending controls. Existing uncertain Starts, Rates and Limits remain held: fresh telemetry or a bridge reboot alone cannot establish safe general BLE-only recovery.

Update through HACS and restart Home Assistant. Firmware compilation and publication do not establish installation or a live charging retest.
