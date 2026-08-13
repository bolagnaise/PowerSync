<!-- release: v2.12.1089 -->

## What's Changed

**Stop failed Sungrow Modbus connections from multiplying retries**

Sungrow SH connections now disable background Modbus reconnects, bound each connection attempt, and close failed or timed-out clients before the next coordinator poll. An unavailable inverter, WiNet-S endpoint, or competing Modbus connection can no longer leave orphaned PowerSync retry loops accumulating in Home Assistant, while healthy connections remain persistent and later polls can still recover automatically.

Update available via HACS
