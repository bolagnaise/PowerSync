<!-- release: v2.12.1700 -->

## Home load stays visible when EV charging data is uncertain

The Right now and Energy flow cards now show the measured house load when PowerSync cannot reliably separate a car's charging power. This reading may include EV charging. The Home Load sensor remains unknown until EV attribution is reliable, so uncertain readings do not enter optimizer load history.

When the Tesla BLE bridge reports a car asleep while its BLE link remains connected, PowerSync treats it as drawing 0 kW. An old Tesla Fleet charging report no longer keeps EV power and Home Load unknown for hours. If the bridge loses its link, the asleep reading is not treated as current evidence.

## Daily energy and costs keep metering

Tesla local Powerwall, Tesla cloud and Sigenergy systems now continue accumulating grid import/export, battery charge/discharge and import costs when Home Load is withheld because EV charging is uncertain. Previously, daily totals and Net cost today could be understated during those gaps. Unknown home consumption remains uncounted; this change does not backfill earlier missing totals.

## EV battery level with OCPP chargers

A single OCPP charger can now use the existing **EV battery SoC sensor** and **Fallback EV battery SoC sensor** settings, even with generic charger control disabled. This lets AC chargers without vehicle battery feedback use your car’s Home Assistant battery-percentage sensor for the EV display, Smart Schedule and target-SoC checks.

A valid battery level from the OCPP connector takes priority, followed by the configured primary and fallback sensors. Both configured sensors must describe the same car. PowerSync does not assign that global sensor pair when multiple OCPP chargers or a separate generic charger are configured. Smart Schedule waits for an available battery level and requests a stop if its session loses that reading.

Update through HACS and restart Home Assistant. Your existing SoC sensor selections are reused; keep generic charger control disabled when OCPP controls your charger.
