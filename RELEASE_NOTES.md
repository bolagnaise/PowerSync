<!-- release: v2.12.1182 -->

## What's Changed

**Tesla retained grid-charge recovery now verifies the cleanup**
When the optimiser has selected self-consumption but Powerwall telemetry still
shows material grid-funded charging above the reserve, PowerSync now explicitly
runs the no-state Tesla recovery path and waits for its confirmed result before
recording the recovery cooldown. This prevents a cleared force-state record
from turning the recovery into a successful-looking no-op, so an unconfirmed
restore remains immediately retryable. Solar-funded charging, reserve recovery,
calibration, native grid-services dispatch, Monitoring Mode, and external or
manual force ownership remain excluded from this safeguard.

Update available via HACS
