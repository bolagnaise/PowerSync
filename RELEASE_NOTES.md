<!-- release: v2.12.1331 -->

## Fixes

### Tesla BLE: unavailable charging power no longer appears as idle

When a Tesla BLE vehicle is actively charging but its measured charge-power
entity is unavailable or stale, PowerSync now keeps the EV source as unknown
instead of classifying the session as idle. The EV status still reports
charging, preserves the unknown measured watts, and continues to withhold
normalized Home Load while the measurement is incomplete. Normal solar/grid
source classification resumes when fresh watts return; requested amps and
surplus estimates are never presented as measured power.
