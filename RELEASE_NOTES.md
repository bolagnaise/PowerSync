<!-- release: v2.12.1595 -->

## Battery energy accounting during delayed SOC updates

Optimizer runs now preserve measured solar and grid energy while waiting for the battery's state of charge to catch up. This prevents repeated solves from gradually turning known energy into unknown energy during normal charging and discharging. Startup, telemetry gaps and later SOC corrections still reconcile conservatively.

Update through HACS and restart Home Assistant. Previously accumulated unknown energy is not reassigned to solar or grid.

## Accurate Tesla home and plug status

Tesla vehicles no longer appear plugged in at home solely from old location and idle charging reports when native cable and connectivity data are unavailable. PowerSync shows unavailable status until fresh evidence arrives, and an online ESPHome Wi-Fi bridge no longer makes a disconnected BLE vehicle appear online or plugged in.

Known cable readings, fresh idle reports and older BLE bridges remain supported. BLE disconnection alone does not establish that a vehicle is away. Update through HACS and restart Home Assistant.
