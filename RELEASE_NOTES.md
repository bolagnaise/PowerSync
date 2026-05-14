<!-- release: v2.12.404 -->

## What's Changed

**Add Sigenergy EVAC and EVDC charger support**
PowerSync can now control Sigenergy EV chargers directly over Modbus. EVAC chargers support start, stop, current-limit control, and charger telemetry; EVDC chargers support start, stop, and telemetry using the registers exposed in Sigenergy's v2.8 protocol. EVDC current limiting is intentionally left disabled until a writable current-limit register is confirmed.

**Show planned charge and discharge windows together**
The dashboard now presents upcoming optimizer force-charge, discharge, and export windows in one planned battery schedule card. The new force-discharge window sensor exposes discharge/export timing, power, duration, and SoC metadata for the dashboard and automations.

Update available via HACS
