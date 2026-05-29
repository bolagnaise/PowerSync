<!-- release: v2.12.499 -->

## What's Changed

**Native 24-hour Smart Optimization dashboard plan**
The Home Assistant dashboard now includes a native 24-hour optimizer plan card that uses the same Smart Optimization API data as the mobile app. It shows the planned SoC path, charge/discharge/export power, EV charging overlays, optimizer reserve, demand windows, import/export price charts, warnings, and a detailed action plan with per-window average, minimum, and maximum import/export pricing. The existing entity-based force-window data remains as a fallback while the full optimizer schedule is unavailable.

**Generic EV charger SoC in aggregate status**
PowerSync now includes configured generic charger SoC sensors when building aggregate EV charging status. Generic charger users, including SolarEdge-style EV SoC sensors, now get the configured SoC value in optimizer/status summaries even when no Tesla Fleet or BLE vehicle SoC sensor is active.

Update available via HACS
