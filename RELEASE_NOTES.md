<!-- release: v2.12.437 -->

## What's Changed

**Fixed optimizer idle being overridden by a nearby force-charge slot**
PowerSync now treats an optimizer `idle` action as a hard stop for active optimizer-owned force charge commands. This prevents Sigenergy and other controlled batteries from continuing to charge just because the next LP charge slot is nearby.

**Preserved the correct charge power during slot-shuffle protection**
When the optimizer keeps an active force-charge through a harmless self-consumption shuffle, it now refreshes the hardware command using the matching lookahead charge slot power instead of the current zero-power action. This avoids accidental fallback to maximum configured charge power.

Update available via HACS
