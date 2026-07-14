<!-- release: v2.12.845 -->

## What's Changed

**Disable Idle no longer holds unnecessarily before reserve-capped exports**

Fixed the Charge By Time deadline projection replaying a planned future export below the optimizer reserve floor. When Disable Idle is enabled, PowerSync now evaluates that future export with the same reserve cap used by the emitted schedule, avoiding an unnecessary IDLE action while still preserving genuine deadline targets.

Update available via HACS
