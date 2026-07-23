<!-- release: v2.12.917 -->

## What's Changed

**No Idle is now available with every electricity provider**
Smart Optimization's No Idle option is no longer limited to a provider allowlist. It is now available in initial setup, integration options, the Home Assistant switch, and the optimization settings API for Amber, Localvolts, Flow Power, GloBird, CovaU, AEMO VPP, Octopus, EPEX, New Zealand, custom tariffs, and future providers.

**Existing optimizer safeguards are preserved**
No Idle still replaces only ordinary optimizer hold periods with self-consumption. Explicit holds required to meet an active Charge By Time target remain IDLE, monitoring mode continues to send no hardware commands, and no battery or inverter control behavior was changed.

**Safe upgrade for previously hidden settings**
The config entry now migrates to version 9. A stale hidden No Idle value from a provider that did not previously expose the option is reset instead of activating silently during the upgrade; after migration, the selected value is preserved when changing electricity providers.

Update available via HACS
