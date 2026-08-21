<!-- release: v2.12.1178 -->

## What's Changed

**Tesla self-consumption now recovers retained grid charging**
When the optimiser has selected self-consumption but live Powerwall telemetry
still shows material grid-funded battery charging above the reserve, PowerSync
now restores the normal Tesla tariff and controls instead of trusting the mode
label alone. The safeguard leaves solar-funded charging, reserve recovery,
calibration, and native grid-services dispatch untouched, and retries if the
physical charging state does not clear.

Update available via HACS
