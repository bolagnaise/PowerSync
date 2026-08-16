<!-- release: v2.12.1119 -->

## What's Changed

**Tesla Home Load now follows the newest physical charging observation**
PowerSync now carries the real source timestamp for VIN-scoped Wall Connector readings through the canonical EV snapshot, public Home Load sensor, paired local Powerwall projection, and Smart Optimization input. An older direct reading can no longer be restamped as current and overwrite a newer same-vehicle stop or restart, preventing stale EV power from clamping positive Home Load to zero.

**Stop/restart identity and fail-closed safety remain consistent**
Current exact-VIN Wall Connector readings update every matching alias, including an explicit `0 W` stop, while a newer vehicle observation wins over stale site telemetry. Other measured chargers remain independent, a distinct active charger without a usable measurement still keeps Home Load and optimizer input unavailable, and no EV or battery command behavior changed.

Update available via HACS
