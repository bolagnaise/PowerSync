<!-- release: v2.12.1129 -->

## What's Changed

**Seasonal tariffs now stay correct across month boundaries**
PowerSync preserves the buy and sell rates for every configured tariff season and re-selects the applicable season for each optimizer interval. A 48-hour plan that crosses into a new month now uses that season's TOU windows and rates, specific seasons take precedence over an `All Year` catch-all, and the live tariff price updates without requiring a Home Assistant reload.

**Automations now support cumulative Export kWh thresholds**
The Home Assistant automation engine can accumulate grid export inside a required local-time window and run the existing conditions and actions once when the configured quota is reached. Import and Export remain direction-specific, use normalized point-of-common-coupling telemetry, persist window progress across restarts, and fall back to bounded live-power integration when a daily counter is unavailable. Counter resets, rollbacks, overnight windows, and recovered telemetry are fenced against double-counting and false threshold spikes.

**Optimizer chart axes use consistent 12-hour labels**
The SOC, Battery Power, and Electricity Price chart axes now display wall-clock labels such as `12:00 AM` and `1:30 PM`. The change is scoped to those axes: timestamps, timezone offsets, DST behavior, tooltips, action ranges, and chart data are unchanged.

**Safety and feature boundaries remain explicit**
This release does not start or stop hardware by itself or change Monitoring Mode, external ownership, action, battery, inverter, vehicle, charger, or optimizer command guards. Cloud automations do not accept the HA-only Export kWh trigger. Automatic retailer-plan authoring, AI interval-level decision provenance, and back-loaded Spread Import are not included; manual and custom tariff authoring remains unchanged.

Update available via HACS
