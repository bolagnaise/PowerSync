<!-- release: v2.12.603 -->

## What's Changed

**Dashboard layout no longer rebalances on every state update**
The PowerSync dashboard now passes Home Assistant state updates through to the cards without rerunning the full masonry layout loop each time sensor data changes. This reduces unnecessary browser work and helps keep the dashboard stable and responsive when live entity updates are frequent.

**Resize relayouts are throttled by meaningful layout changes**
Dashboard relayouts now run when the column count changes or the container width moves enough to matter, instead of responding to every small resize observation. The dashboard JavaScript cache version was also bumped so browsers load the updated layout behavior after the HACS update.

Update available via HACS
