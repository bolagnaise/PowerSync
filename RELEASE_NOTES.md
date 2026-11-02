<!-- release: v2.12.1594 -->

## Accurate Tesla home and plug status

Tesla vehicles no longer appear plugged in at home solely from old location and idle charging reports when native cable and connectivity data are unavailable. PowerSync shows unavailable status until fresh evidence arrives, and an online ESPHome Wi-Fi bridge no longer makes a disconnected BLE vehicle appear online or plugged in.

Known cable readings, fresh idle reports and older BLE bridges remain supported. BLE disconnection alone does not establish that a vehicle is away. Update through HACS and restart Home Assistant.
