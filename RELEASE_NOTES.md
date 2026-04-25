## What's Changed

**FoxESS H3-Smart: Corrected registers and extended battery health data**
Several Modbus register addresses for the H3-Smart were incorrect, causing wrong battery voltage, current, and temperature readings. All three are now mapped to the correct BMS registers per the FoxESS Modbus Protocol spec. Additionally, the H3-Smart now reports State of Health (SOH), rated battery capacity (kWh), total lifetime charged energy, and max charge/discharge power — all surfaced in the battery health panel and force-mode power slider in the app.

**Update available notification**
When a new PowerSync version is detected (checked on startup and every hour), a persistent notification now appears in the HA sidebar bell with a link to the release notes. The notification is shown once per version to avoid repeat alerts.

**Monitoring mode: PS automations no longer override manual settings**
When PS Smart Optimization is in monitoring mode, any PS automation that controls the battery or grid (set_grid_export, set_backup_reserve, force_charge, etc.) was still executing and silently overriding user-configured settings. These 18 control action types are now blocked while monitoring mode is active — the optimizer observes only.

**Manual grid export setting survives config reloads**
The "manual grid export override" flag (set when you explicitly choose a grid export mode in the app) was stored only in memory and lost whenever the config entry reloaded — which happens every time monitoring mode is toggled. The override and its rule are now persisted to HA storage and restored on reload, so your manual setting is respected across restarts and monitoring mode changes.

**Flow Power import/export cost tracking corrected**
Import Cost Today and Export Earnings Today were showing approximately 1/100th of the correct value for Flow Power users (e.g., $0.02 instead of ~$1.75 for 5.86 kWh). The root cause: tariff schedule prices are stored internally in $/kWh (Tesla format) but were being treated as cents/kWh, creating a 100× undercount. Additionally, the live price lookup now applies the full PEA formula for Flow Power users when reading from the AEMO coordinator, so cost tracking works correctly even before the first tariff sync.

Update available via HACS
