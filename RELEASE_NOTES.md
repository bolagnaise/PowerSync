## What's Changed

**Fix: Flow Power export earnings tracking was using the wrong price**
The `Daily Export Earnings` sensor was accumulating export credit at the AEMO wholesale spot price (~7c/kWh) instead of Flow Power's flat 45c/kWh Happy Hour rate. A user who exported 32 kWh during Happy Hour would see $2.32 instead of the correct $14.57. The accumulator now uses the same Happy Hour rate logic that the `Flow Power Export Price` sensor already correctly displays. Earnings reset at midnight as normal — corrected values take effect from the next midnight rollover or HA restart.

**Fix: Non-Tesla inverters sent wrong power units to the EV charging planner**
Sigenergy, Sungrow, FoxESS, GoodWe, AlphaESS, and SolaX coordinators all report power in kW, but the EV planner endpoint was expecting watts. This caused the planner to see load, solar, and grid values ~1000× too small, leading to incorrect charge scheduling decisions for non-Tesla battery systems. Values are now converted correctly before being passed to the planner.

**Fix: Sigenergy force charge now uses PV-first mode**
When triggering a forced charge on Sigenergy inverters, the integration was setting CHARGE_GRID mode (mode 3), which suppresses solar generation and charges exclusively from the grid. It now correctly uses CHARGE_PV mode (mode 4), which charges from solar first and supplements with grid as needed — the intended behaviour for a daytime charge command.

**Fix: OCPP EV charger status with HACS lbbrhzn/ocpp integration**
The lbbrhzn/ocpp HACS integration exposes two status sensors per charger: a charge-point level `*_status` (which often stays "unknown") and a connector-level `*_status_connector` (which updates reliably). PowerSync now falls back to the connector sensor when the primary is unavailable. Additionally, the EV plug detection logic now correctly reads `*_status_connector` entities from the HACS OCPP platform to determine whether a car is present, enabling the EV charging planner to work properly with HACS-based OCPP setups.

*Update available via HACS*
