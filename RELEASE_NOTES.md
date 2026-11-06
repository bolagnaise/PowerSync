<!-- release: v2.12.1647 -->

## Tesla BLE recovery with a sleeping vehicle

The Stop recovery companion could discard fresh charge-state queries while a vehicle was asleep, leaving recovery waiting for proof. Explicit recovery now uses the existing authenticated wake path to request those readings. Missing or incomplete stopped/zero readings still keep recovery held.

**Update both PowerSync version references to v2.12.1647 in each original bridge’s YAML, then rebuild and install its firmware using the [recovery guide](https://github.com/bolagnaise/PowerSync/blob/v2.12.1647/docs/tesla-ble-stop-recovery.md).** Updating HA alone does not change bridge firmware. Preserve pairing, keys, VIN mappings and recovery files, and keep EV automations and other writers paused during recovery. Update PowerSync through HACS and restart HA before retrying the same complete inventory.
