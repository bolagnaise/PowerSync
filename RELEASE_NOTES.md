<!-- release: v2.12.1357 -->

## What's Changed

**Keep Fronius storage entities scoped to the selected integration**
PowerSync now resolves Fronius GEN24 telemetry and storage-control entities from the selected `fronius_modbus` config entry before considering unregistered legacy entity IDs. Entities owned by another config entry are ignored, preventing cross-device reads/writes and false load-following convergence in multi-entry Home Assistant setups.

Update available via HACS
