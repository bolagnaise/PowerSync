<!-- release: v2.12.765 -->

## What's Changed

**Sungrow self-consumption restore now retries discharge-cap recovery**
PowerSync now keeps the intended Sungrow discharge-limit restore target when a temporary Modbus write fails, so a later restore/reload can retry instead of forgetting the normal inverter limit. This helps recover systems that briefly reject the max-discharge register while coming out of force, idle, or no-discharge modes.

**SH10RS/SBH restore handles unreadable cap registers more safely**
For Sungrow firmware that does not expose the writable discharge-cap register, PowerSync can now use the live telemetry pattern of high battery state of charge, grid import, and near-zero battery output to identify a likely stuck discharge cap and restore it from the known normal limit.

Update available via HACS
