<!-- release: v2.12.1699 -->
## EV battery level with OCPP chargers

A single OCPP charger can now use the existing **EV battery SoC sensor** and **Fallback EV battery SoC sensor** settings, even with generic charger control disabled. This lets AC chargers without vehicle battery feedback use your car’s Home Assistant battery-percentage sensor for the EV display, Smart Schedule and target-SoC checks.

A valid battery level from the OCPP connector takes priority, followed by the configured primary and fallback sensors. Both configured sensors must describe the same car. PowerSync does not assign that global sensor pair when multiple OCPP chargers or a separate generic charger are configured. Smart Schedule waits for an available battery level and requests a stop if its session loses that reading.

Update through HACS and restart Home Assistant. Your existing SoC sensor selections are reused; keep generic charger control disabled when OCPP controls your charger.
