<!-- release: v2.12.555 -->

## What's Changed

**Faster mobile app startup after Home Assistant restarts**
The Home Assistant backend configuration endpoint now registers before PowerSync starts slower network and coordinator warmup work. This lets the mobile app detect the integration immediately instead of waiting behind Amber price fallback, Tesla site refreshes, Powerwall local checks, or capability probing during startup.

**Startup responsiveness guard**
Added regression coverage to keep the mobile app probe ahead of slow startup refreshes, so future startup changes do not accidentally reintroduce app load timeouts.

Update available via HACS
