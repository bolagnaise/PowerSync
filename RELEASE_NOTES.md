<!-- release: v2.12.1118 -->

## What's Changed

**Tesla charging transitions now stay consistent across every Home Load consumer**
PowerSync now applies the current VIN-scoped Wall Connector reading before storing the canonical EV snapshot and before Smart Optimization reads energy telemetry. This closes the remaining path where a fresh cached vehicle value could reintroduce the previous charging power after a stop, or omit the new charging power after a restart, even though the public Home Load sensor already had the corrected direct reading.

**Exact physical identity and fail-closed safety are unchanged**
Only the matching physical vehicle is replaced. Other measured chargers remain in the total, a different active charger without a usable measurement still keeps Home Load and optimizer input unavailable, and no EV or battery command behavior is changed.

Update available via HACS
