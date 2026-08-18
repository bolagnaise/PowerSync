<!-- release: v2.12.1136 -->

## What's Changed

**Unavailable Home Load no longer appears as 0 W**
The canonical EV/loadpoint snapshot now applies EV normalization before publishing the site values, preserves a genuinely unavailable Home Load as unavailable, and recalculates surplus from that normalized snapshot. The built-in energy-flow card shows `--` and suppresses Home flow animation while the source is unknown; a real measured zero still displays as 0 W. This closes the remaining display path where fail-closed Sungrow telemetry could be mistaken for a zero household load.

**Tesla Price Level starts now require physical confirmation**
Price Level charging now waits for fresh, exact-vehicle charging state plus measured draw before recording a Tesla session or ownership lease. If Tesla accepts a start command but the car does not begin drawing power, PowerSync leaves the session inactive, uses the existing compensating-stop cleanup, and retries with bounded backoff from 30 seconds up to 15 minutes while the price remains eligible. Zaptec and other charger backends retain their existing confirmation behavior and diagnostics.

Update available via HACS
