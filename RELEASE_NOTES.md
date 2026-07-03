<!-- release: v2.12.755 -->

## What's Changed

**Fixed EV Smart Schedule planning in local time**
PowerSync now normalizes EV forecast and departure-time comparisons to Home Assistant's configured local timezone. This prevents Smart Schedule from dropping valid overnight charging windows when the Python runtime or upstream forecast timestamps use a different timezone.

**Improved dynamic price forecast handling**
Amber and Flow Power forecast timestamps are now converted to Home Assistant local time before the EV planner builds charge windows, so UTC provider timestamps no longer appear as stale local slots.

Update available via HACS
