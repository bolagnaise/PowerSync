<!-- release: v2.12.1787 -->

## Battery optimizer recovery

If the optimizer cannot find a usable plan, PowerSync returns the battery to self-consumption after releasing any optimizer-owned force mode. The dashboard shows **Optimizer degraded** while the optimizer retries. A prolonged solver failure can delay planned charging or cause a Charge By Time target to be missed.

## Tessie Price Level charging

When Price Level takes over a compatible Solar Surplus session, PowerSync can keep the Tesla charging. It checks fresh Tessie readings for the same car and verifies the current fits the incoming plan.

Update through HACS and restart Home Assistant.
