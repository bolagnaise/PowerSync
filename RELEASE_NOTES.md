<!-- release: v2.12.914 -->

## What's Changed

**Reliable Powerwall exit from planned export**
When Smart Optimization returns a Tesla Powerwall to self-consumption, PowerSync now uses the confirmed local-first mode path and rechecks export safety before every cloud fallback or retry. This prevents an accepted cloud request from masking a Powerwall that remains in Autonomous/export mode.

**SolaX current limits restored after forced operation**
SolaX manual-profile force charge and force discharge now preserve the original maximum current across repeated optimizer commands and Home Assistant restarts, then restore it before returning the inverter to Self Use. If the current entity is temporarily unavailable, the configured safe limit is retained as the fallback.

Update available via HACS
