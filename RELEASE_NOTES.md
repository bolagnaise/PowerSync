<!-- release: v2.12.1073 -->

## What's Changed

**Tesla BLE now honours proven low-current capability**

Tesla BLE and Teslemetry Bluetooth charge-current entities now use their
advertised positive minimum just like cloud vehicle control. When the entity
reports a minimum of 0 A, PowerSync can use 1 A as the lowest active charge
rate, including during the Solar Surplus stop delay. If the current entity is
missing, unavailable, or does not expose valid bounds, PowerSync retains the
conservative 5 A fallback.

This corrects the overly conservative BLE limitation described in the previous
release notes; supported BLE control paths can also charge below 5 A.

Update available via HACS
