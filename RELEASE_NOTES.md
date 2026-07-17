<!-- release: v2.12.873 -->

## What's Changed

**Reliable mixed Tesla and BLE charging**
PowerSync no longer starts a Tesla through its BLE gateway and then immediately stops the same charger when the SOC-less BLE alias is discovered beside the real vehicle. The duplicate alias is now non-actionable and cannot issue competing charger commands.

**Faster Sungrow recovery after network outages**
When the core Sungrow battery register read fails, PowerSync now aborts the remaining optional reads, resets the Modbus connection, and retries with a fresh connection on the next cycle. This prevents a brief network outage from holding the Sungrow command path for minutes while sequential register timeouts accumulate.

Update available via HACS
