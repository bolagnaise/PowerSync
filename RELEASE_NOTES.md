<!-- release: v2.12.1644 -->

## Retry recovery after an interrupted Tesla BLE Stop

BLE-only Tesla recovery could keep returning `recovery_scope_changed` after another unconfirmed Stop was saved during an attempt. A retry of the same complete inventory now checks the added Stops against the original bridges and vehicles, then requires fresh proof from every bridge before releasing the hold. This also covers records already left held by an earlier attempt.

Update through HACS and restart Home Assistant. Keep the existing pinned companion firmware and follow the [BLE-only Stop recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1644/docs/tesla-ble-stop-recovery.md), including pausing EV automations and other writers. Changed bindings, missing history and non-Stop commands remain held for review.

## Reliable live energy and cost checkpoints

Frequent telemetry updates could keep postponing the save of daily and month-to-date energy and costs, allowing totals to roll back after an interrupted Home Assistant restart. PowerSync now saves the latest totals at regular five-minute checkpoints while readings continue to arrive.

An abrupt stop can still lose readings since the latest checkpoint, and updating cannot recover previously lost measurements. Live estimates remain separate from Amber's metered billing totals. Update through HACS and restart Home Assistant.
