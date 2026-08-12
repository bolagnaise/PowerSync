<!-- release: v2.12.1080 -->

## What's Changed

**Solar Surplus charging now resumes safely after a Home Assistant restart**
PowerSync now reclaims its own in-progress Tesla Solar Surplus session after a restart when the saved session is recent, belongs to the exact vehicle, and live telemetry still matches the active charging state and last commanded current. This prevents PowerSync from mistaking its uninterrupted charge for an external manual start and losing automated rate control or Stop Delay behavior.

Stale, stopped, changed-current, non-Tesla, and ambiguous sessions remain unclaimed so external or manual charging is not taken over.

Update available via HACS
