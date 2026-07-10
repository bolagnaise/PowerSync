<!-- release: v2.12.814 -->

## What's Changed

**Preserved low-SOC recovery holds when No Idle is enabled**
When the optimiser plans a later grid charge while the battery is already near its hardware reserve, PowerSync now keeps the protective hold before that charge instead of converting it to self-consumption. This prevents the battery draining through the planned floor and avoids unnecessary grid imports before the recovery charge begins.

**Kept generic force-discharge events active for their full window**
Generic battery force-discharge control now re-arms the hardware command while an event remains active, preventing inverter-side command timeouts from ending a scheduled discharge early.

Update available via HACS
