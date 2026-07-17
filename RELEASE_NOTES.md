<!-- release: v2.12.874 -->

## What's Changed

**Accurate Tesla BLE SOC without duplicate charger commands**
Price-level EV charging now reads the configured ESPHome Tesla BLE charge-level sensor directly. When a real Tesla vehicle and its first BLE gateway are both discovered, PowerSync keeps the BLE telemetry but treats that gateway as the same control path, preventing duplicate starts or a competing stop command even when BLE SOC is available.

**Stable optimizer projection for long self-consumption runs**
Equivalent plans with the same command modes now tolerate up to 20 Wh of natural self-consumption boundary drift per continuous run. This prevents repeated projection non-convergence warnings on long schedules while preserving strict rejection of command-mode changes and larger energy differences.

Update available via HACS
