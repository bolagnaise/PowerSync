<!-- release: v2.12.815 -->

## Fixes

- Fixed Tesla/Powerwall backup reserve readback so fresh local control data, or an in-flight local reserve write, wins over stale Fleet site info. This prevents Smart Optimization from re-adopting an old higher reserve immediately after a user lowers the reserve through PowerSync.
