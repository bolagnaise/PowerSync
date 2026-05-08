<!-- release: v2.12.332 -->

## What's Changed

**NeoVolt high-stack parking**
When independent NeoVolt / Bytewatt stacks drift beyond the configured SOC tolerance, PowerSync now actively parks the higher-SOC stack in `No Battery Charge` so the lower-SOC stack can catch up. This fixes the case where both stacks were left in `Normal` and the higher-SOC pack continued charging while the lower pack discharged.

**Automatic restore after balancing**
If PowerSync parked a stack for SOC balancing, it restores that stack to its previous normal mode once the packs are back within tolerance.

Update available via HACS
