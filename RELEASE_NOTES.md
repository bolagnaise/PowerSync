<!-- release: v2.12.1650 -->

## Recover battery charging blocked by an EV reservation

A new **Recover EV charging headroom** action can request fresh Tesla vehicle and local Powerwall meter readings when a retained EV reservation blocks battery grid charging. It checks that the vehicle is stopped before releasing that reservation, so recovery can be attempted without waiting for the provider’s cached data to advance or collecting another log first.

Update through HACS and restart Home Assistant. In Developer Tools → Actions, select **PowerSync: Recover EV charging headroom**, enter the affected VIN and enable **Allow provider refresh**. The action makes up to two vehicle reads; Teslemetry refreshes can use credits. It does not wake the car or start charging, and leaves the reservation held if fresh, consistent readings cannot be obtained. After a successful response, retry battery charging or wait for the next optimizer cycle.
