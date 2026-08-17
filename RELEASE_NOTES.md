<!-- release: v2.12.1133 -->

## What's Changed

**Tesla deadline charging now restarts after a planned pause**
When Fleet, Teslemetry, and a paired BLE bridge update at different speeds, PowerSync now follows the freshest same-vehicle charging state and treats recent provider disagreements conservatively. A stale cloud `charging` reading can no longer make Smart Schedule skip the physical restart command after a newer provider reports the vehicle stopped. This applies to Tesla charging through a Wall Connector or UMC and remains scoped to the exact vehicle in multi-Tesla and multi-connector homes.

**Charge-by-time uses every remaining minute**
Time-critical plans no longer discard a final charging interval just because fewer than six minutes remain before departure. If the vehicle is still below its target, PowerSync keeps the valid remainder of the deadline window available instead of stopping early.

**Physical confirmation remains fail-closed**
A Tesla start now requires a fresh same-vehicle measured-current or measured-power update as well as a current charging state. Stale draw from another delayed provider cannot confirm a restart.

Update available via HACS
