<!-- release: v2.12.1069 -->

## What's Changed

**Deferred AEMO tariff syncs now stay on Home Assistant's event loop**

When an Amber, Flow Power, Localvolts, or AEMO sensor dispatch arrives while
Home Assistant is still starting, PowerSync now schedules the deferred tariff
sync on Home Assistant's event loop. This prevents the startup thread-safety
error that could discard the first settled-price sync while preserving the
existing reload and unload task safeguards.

Update available via HACS
