<!-- release: v2.12.1276 -->

## What's Changed

**Sungrow Controls Unlimited no longer enables a 50 W export cap**
Controls requests from older mobile clients that represent Unlimited as `0` now disable the Sungrow export limiter, matching the documented Unlimited behavior. PowerSync's separate internal zero-export safety path remains unchanged.

**Generic Chargers can create their first Smart Schedule**
A configured Generic Charger now appears as a disabled Smart Schedule candidate before any schedule has been saved. You can set departure times and explicitly enable it from the existing screen; simply showing the candidate never starts charging or sends a charger command.

Update available via HACS
