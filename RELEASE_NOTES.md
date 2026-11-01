<!-- release: v2.12.1591 -->

## Tesla charging confirmation

Tesla Starts now use newer charging or stopped readings from the same vehicle and provider while confirmation is pending. In Fleet API or Both mode, PowerSync can also request a bounded fresh Fleet read when Home Assistant's cached vehicle data is too old, reducing failed Start reports when the car has begun charging.

Fresh charging state and measured current or power are still required. Update through HACS and restart Home Assistant; no firmware update or re-pairing is required.
