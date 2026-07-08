<!-- release: v2.12.793 -->

## What's Changed

**Manual optimizer reserve now controls export floors**
When Auto-Apply Optimizer Reserve is disabled, PowerSync no longer applies the hidden home-load bridge export floor that can be generated after an optimizer solve. This keeps Happy Hour/export plans aligned with the manually restored optimizer reserve shown in Smart Optimization, instead of stopping export early at a higher internal bridge floor.

**Cached force-action regression aligned**
Updated the broader export regression suite to match the shipped cached-command behavior from `v2.12.792`: cached force charge/discharge/export actions wait for a fresh optimizer solve at schedule boundaries.

Update available via HACS
