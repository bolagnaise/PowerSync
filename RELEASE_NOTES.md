## What's Changed

**FoxESS H3-Smart: corrected Modbus registers for battery voltage, current, and temperature**
The H3-Smart was reading from the wrong registers for three core BMS values — battery voltage was mapped to a register shared with H3-Pro (and wrong for H3-Smart), current and temperature were swapped. All three now point to the correct addresses per FoxESS Modbus Protocol V1.05.03.00, so voltage, current, and temperature readings will be accurate for the first time on this model.

**FoxESS H3-Smart: SOH, rated capacity, and lifetime charge energy**
State of Health (%), nominal battery capacity (kWh), and total lifetime charged energy (kWh) are now read directly from the H3-Smart's Modbus registers and surfaced in the battery health API. The mobile app's battery health screen will now show these fields for H3-Smart inverters.

**FoxESS: max charge/discharge power now taken from inverter rating**
Previously the force charge/discharge slider maximum was estimated by multiplying BMS current × pack voltage, which produced inaccurate results depending on live conditions. It's now read directly from the inverter's rated power register (e.g. 15.0 kW for a 15 kW H3-Smart). The `/api/power_sync/foxess_settings` response includes `battery_max_charge_power` and `battery_max_discharge_power` so the mobile app can render the correct limit.

**FoxESS: battery temperature now polled for all models**
Battery temperature was defined in the register map for all FoxESS model families but was never actually read from the inverter. It's now polled in every status cycle and included in the battery health API response.

**FoxESS H3-Smart: force charge/discharge race condition fix**
On H3-Smart inverters, the power setpoint could be silently dropped if it arrived before the inverter finished processing the remote enable signal. The post-enable delay has been increased from 0.5 s to 1.0 s to give the firmware time to become ready.

**Update notification in HA sidebar**
PowerSync now creates a persistent notification in the Home Assistant sidebar when a new version is available. The notification appears on startup and on the hourly update check, includes a link to the release notes, and won't repeat for the same version within a session.

*Update available via HACS*
