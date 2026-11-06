<!-- release: v2.12.1644 -->

## Retry recovery after an interrupted Tesla BLE Stop

BLE-only Tesla recovery could keep returning `recovery_scope_changed` after another unconfirmed Stop was saved during an attempt. A retry of the same complete inventory now checks the added Stops against the original bridges and vehicles, then requires fresh proof from every bridge before releasing the hold. This also covers records already left held by an earlier attempt.

Update through HACS and restart Home Assistant. Keep the existing pinned companion firmware and follow the [BLE-only Stop recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1644/docs/tesla-ble-stop-recovery.md), including pausing EV automations and other writers. Changed bindings, missing history and non-Stop commands remain held for review.
