<!-- release: v2.12.1217 -->

## What's Changed

**Zero-FIT battery holds now retain their RTE-aware plan through projection**
When the optimiser deliberately holds battery energy while the home imports,
then plans a later grid recharge that is not cheap enough to recover the
round-trip loss, it now keeps that earlier slot as a hold. This prevents a
zero or negative feed-in tariff from being remapped to natural self-consumption
and creating an avoidable discharge/recharge cycle on the next optimiser pass.

Valid cycles remain unchanged when a genuinely cheaper later recharge makes
the round-trip loss economic.

Update available via HACS
