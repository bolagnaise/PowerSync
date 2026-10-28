<!-- release: v2.12.1522 -->

## Fixed

### Tesla EV charging commands

Fixed Tesla charging starts and current changes failing with a `NoneType.lower` error after an earlier start had no usable current reading, including after a Home Assistant restart. This affects both Tesla BLE and Tesla API charging.

Vehicle wake and fresh charging confirmation are still required. Update through HACS, restart Home Assistant, then retry charging.
