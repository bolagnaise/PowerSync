<!-- release: v2.12.1839 -->

## EV headroom recovery with the Teslemetry HACS integration

If you use Teslemetry's own HACS release instead of the built-in Home Assistant Teslemetry integration, **Recover EV charging headroom** always failed with `fresh_readback_unavailable` and `vehicle_read.reason: cloud_reader_unavailable`, even when the car was awake. PowerSync now accepts the car reading from the Teslemetry HACS release in the same way as from the built-in integration. Recovery still releases a reservation only after two fresh readings show the car has stopped.

Update through HACS and restart Home Assistant.
