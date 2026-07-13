<!-- release: v2.12.835 -->

## What's Changed

### Stable Auto-Apply reserve buffers

Auto-Apply Optimizer Reserve now treats the selected minimum discharge level as the buffer to retain until the next forecast charging opportunity. It adds expected net household load from the end of the full eligible export window, producing the same calculated reserve regardless of the previous reserve value. This prevents a low starting value from collapsing to 0% and a higher value from ratcheting upward, while leaving the hardware backup reserve unchanged.

### No Idle plans now match execution

When No Idle is enabled, ordinary hold slots are now modeled as self-consumption before the schedule is published. The Action Plan, battery-power graph, decisions log, and hardware action therefore show the same behavior. Explicit Charge By Time holds remain Idle so the battery still preserves the energy needed to meet its configured deadline.

Update available via HACS
