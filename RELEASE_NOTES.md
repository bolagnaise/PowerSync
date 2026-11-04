<!-- release: v2.12.1627 -->

## More accurate Tesla EV status

The displayed update time now follows newer vehicle-state reports, even when charging power has not changed. When Tesla Fleet and BLE are used together, battery percentage follows the newest available battery reading for that car.

Expired power readings remain unavailable. Battery percentage can still be a last-known value until the car sends another battery report.

Update through HACS and restart Home Assistant.
