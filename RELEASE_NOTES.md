<!-- release: v2.12.1116 -->

## What's Changed

**Teslemetry Energy Site streaming no longer delays Home Assistant startup**
PowerSync now registers its lifetime Teslemetry SSE reconnect loop as a Home Assistant background task. Home Assistant no longer waits for that intentionally long-running telemetry task during bootstrap, avoiding the repeatable five-minute startup timeout seen on Teslemetry-backed Powerwall systems.

**Telemetry behavior and clean shutdown are preserved**
The stream keeps its existing name, event handling, reconnect behavior, REST fallback, and explicit config-entry unload cancellation. This changes task lifecycle bookkeeping only; it does not add hardware commands or alter optimizer ownership.

Update available via HACS
