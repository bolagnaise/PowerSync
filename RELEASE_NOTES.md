## What's Changed

**EU & Asia-Pacific Tesla Fleet API Support**
Tesla returns a 421 "out of region" error when EU or AP users try to authenticate through the North American Fleet API endpoint. PowerSync now automatically detects this, extracts the correct regional endpoint from the error response, and retries — storing the regional URL for all future API calls. Previously, setup silently failed for EU/AP users with no actionable error message.

**Sigenergy Force Charge: Solar-First Mode**
Sigenergy batteries were previously force-charged using grid-only mode (CHARGE_GRID), which suppresses solar generation entirely while charging. This has been switched to PV-first mode (CHARGE_PV), which uses available solar and only draws from the grid to make up the difference. This means scheduled charging sessions now work with your solar instead of against it.

**Flow Power Export Pricing Fix**
The LP optimizer was using the AEMO spot price to value Flow Power solar exports, which is incorrect — Flow Power pays a fixed happy-hour rate (not the spot price) and nothing outside happy hour. The optimizer now uses the actual happy-hour flat rate when applicable and zero otherwise. This fixes a situation where the optimizer was misjudging the value of exporting solar, potentially leading to suboptimal charge/discharge decisions.

**OCPP Charger Support via lbbrhzn/ocpp Integration**
EV charging detection and price-level charging control now work with the popular HACS `lbbrhzn/ocpp` integration, which uses `sensor.*_status_connector` entities. Previously only the built-in OCPP server was recognised. Switch-based charger control (Generic Charger) has also been added to the price-level charging executor.

**EV Auto-Schedule Respects Opportunistic Price Threshold**
When the auto-schedule has planned windows at high prices (e.g. 30c overnight), its opportunistic logic could trigger grid charging at any price below 20c — ignoring the tighter threshold configured in your price-level settings (e.g. 5c). The auto-schedule now defers to your price-level policy for opportunistic (not in-window) grid charging. Planned charging windows are unaffected.

**Tesla Signaling: Circuit Breaker for Invalid Tokens**
After 3 consecutive failed attempts to exchange a token for hermes signaling, PowerSync now stops retrying instead of flooding the log. `psync_` proxy tokens are not accepted by the hermes endpoint; PowerSync now preferentially uses the `tesla_fleet` Home Assistant integration token when available, which carries the correct scope.

Update available via HACS
