<!-- release: v2.12.1641 -->

## Recovery for held BLE-only Tesla Stops

BLE-only Tesla setups now have an explicit recovery route for retained, unconfirmed Stop commands. It checks every affected bridge and vehicle together, verifies fresh stopped and zero-power readings after a bridge restart, and keeps the history of the earlier command outcomes intact.

This requires the pinned PowerSync companion ESPHome component and confirmation of the complete affected vehicle inventory. Updating PowerSync alone will not clear the hold. Mixed Start/current/limit history, changed pairing or missing proof stays held; recovery does not replay an earlier Start.

Update through HACS and restart Home Assistant, then follow the [BLE-only Stop recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1641/docs/tesla-ble-stop-recovery.md). This adds a recovery capability; it does not establish the cause of an earlier communication failure.
