<!-- release: v2.12.1222 -->

## What's Changed

**Amber price-stream reload cleanup**

PowerSync now cancels and waits for an in-flight Amber WebSocket price fetch when the integration stops or reloads. A stopped client cannot retain a receive, cache a late price, or trigger a later synchronisation after its replacement starts.

**Clearer Amber fallback health**

When Amber's optional price stream is unavailable, PowerSync continues using the existing REST price fallback and records one actionable warning per outage rather than repeating a warning every five-minute interval. A successful stream price resets that warning for a later outage.

Update available via HACS
