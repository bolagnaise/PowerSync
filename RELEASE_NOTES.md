<!-- release: v2.12.1114 -->

## What's Changed

**Stopped Teslas no longer appear to be charging from Wall Connector auxiliary draw**
PowerSync now keeps a fresh Tesla `stopped` state authoritative when a connected Wall Connector is only supplying sub-minimum auxiliary power. The vehicle remains connected and idle at 0 W in the dashboard instead of appearing to charge at roughly 500 W, while the real auxiliary draw remains included in Home Load accounting.

**Smart Schedule no longer treats the same auxiliary draw as an active charge session**
The independent Tesla charging probe now distinguishes low auxiliary consumption from viable AC charging power, preventing repeated stop attempts after the vehicle has already stopped. Higher measured power can still override stale provider state when genuine charging is occurring.

Update available via HACS
