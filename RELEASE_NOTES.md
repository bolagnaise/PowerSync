<!-- release: v2.12.1786 -->

## Tesla BLE Scheduled Charging

Scheduled Charging can retry a missed start when fresh readings from the configured Tesla BLE bridge show the car is stopped and drawing no power. This covers BLE-only setups and VIN-linked cars with a paired bridge when the native charging status is unavailable or stale. Fresh native readings that indicate charging or positive draw keep the current session in place.

Update through HACS and restart Home Assistant.
