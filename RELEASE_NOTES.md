<!-- release: v2.12.1171 -->

## What's Changed

**GoodWe DC Solar Curtailment now requires verified inverter settings**
Direct GoodWe export-limit writes are read back before PowerSync marks DC
Solar Curtailment Active. If the inverter acknowledges a write without the
expected register values, the dashboard stays Pending instead of claiming
that export has stopped.

**GoodWe curtailment restores the original export settings after restart**
PowerSync now persists the pre-curtail export-limit baseline while a direct
GoodWe zero-export limit is active. A Home Assistant restart/reload retains
that baseline, verifies the next command afresh, and restores the original
settings when the tariff releases the limit.

Update available via HACS
