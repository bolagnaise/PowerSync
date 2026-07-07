<!-- release: v2.12.784 -->

## What's Changed

**Keep Flow Power Happy Hour out of Idle**
Flow Power Happy Hour export windows are now treated as priority export windows even when Profit Max is not enabled. This prevents Smart Optimization from emitting an Idle hold during the active 17:30-19:30 export period when a positive Happy Hour feed-in rate is configured, so external-control setups should see export or self-consumption instead of a five-minute Idle import gap.

Update available via HACS
