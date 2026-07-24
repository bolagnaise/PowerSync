<!-- release: v2.12.926 -->

# PowerSync v2.12.926

## Fixed

- Restored config-entry startup after v2.12.924 introduced an early state write that could raise `KeyError: 'power_sync'` before Home Assistant finished initializing PowerSync. This affected every provider, although Flow Power users commonly saw it immediately after the KWatch account-summary log.
- Fixed Tesla Hold SoC so the required dispatch-wake reserve pulse returns to the requested hold floor instead of the optimizer's configured reserve.
- Added bounded reserve readback, supersession protection, crash-safe pending cleanup, and verified cleanup retries so Hold SoC is only reported active after the final Tesla reserve is confirmed and never overwrites a newer manual reserve command.

Update available via HACS
