<!-- release: v2.12.643 -->

## What's Changed

**Show effective optimizer action after reserve safety overrides**
When the optimizer blocks a planned export or discharge slot because the projected battery SOC would fall below the optimizer reserve, PowerSync now reports the effective runtime action as the current action instead of continuing to show the planned schedule action. This keeps the dashboard, API, and Current Action sensor aligned with what PowerSync actually executed while still exposing the planned current action in sensor/API attributes for troubleshooting.

Update available via HACS
