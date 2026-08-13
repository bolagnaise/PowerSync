<!-- release: v2.12.1085 -->

## What's Changed

**Keep simultaneous EV charging modes aligned with the session they own**
Price-Level Charging now appears active only after it successfully acquires the vehicle's charging session. When Smart Schedule already owns the same vehicle, a blocked Price-Level start no longer creates false active state or later blocked stop attempts, and a completed Price-Level stop is not sent a second time. Scheduled Charging, multiple vehicles, and standalone charger ownership remain unchanged.

Update available via HACS
