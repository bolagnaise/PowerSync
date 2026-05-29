<!-- release: v2.12.500 -->

## What's Changed

**Prevent optimizer charge windows from being interrupted**

PowerSync now skips periodic TOU tariff sync while the LP optimizer owns an active force-charge or force-discharge command. This prevents Sigenergy Modbus charge windows from being restored back to self-consumption early during price refreshes, so planned battery charge windows can continue for their intended duration instead of stopping after short bursts.

Update available via HACS
