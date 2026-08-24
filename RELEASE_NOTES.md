<!-- release: v2.12.1190 -->

## What's Changed

**Reconcile a charging Tesla before applying a zero-amp Smart Schedule plan**
When a Tesla is already physically charging as Smart Schedule begins, PowerSync now seeds the managed session from fresh vehicle-specific current telemetry instead of assuming the deferred 0 A optimizer target has already reached the charger. The next controller update can therefore reduce the real charging current and preserve the planned home-battery charge rate and site import limit.

PowerSync still yields to Tesla app or vehicle-schedule sessions that are externally owned, ignores stale post-stop telemetry, and sends no start or rate command when the vehicle is already stopped at the optimizer's 0 A target.

Update available via HACS
