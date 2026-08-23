<!-- release: v2.12.1187 -->

## What's Changed

**Tesla app charging remains under external control**
When Tesla charging starts outside PowerSync, including from the Tesla app or the vehicle's own schedule, PowerSync now records a vehicle-specific external owner before Smart EV Charging evaluates it. Smart Schedule, Price-Level, Scheduled Charging, and Solar Surplus yield instead of stopping that session.

External control remains active until the vehicle is unplugged, while explicit PowerSync Manual and Boost commands can still take control. VIN isolation plus restart and command-settle guards prevent another vehicle or delayed Tesla telemetry from claiming the wrong session.

Update available via HACS
