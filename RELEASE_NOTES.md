<!-- release: v2.12.795 -->

## What's Changed

**Smart Optimization automations now re-enable correctly after a restart**
Automations that disable Smart Optimization, restart or update Home Assistant while it is disabled, and then re-enable it now bring the optimizer back online instead of only flipping the saved setting. PowerSync now lets the integration reload when no optimizer coordinator exists, so the LP coordinator and optimizer sensors are recreated correctly.

Update available via HACS
