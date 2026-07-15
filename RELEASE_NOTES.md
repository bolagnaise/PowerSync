<!-- release: v2.12.855 -->

## What's Changed

**Stable optimizer actions at five-minute boundaries**
PowerSync now keeps the cached boundary action authoritative for its five-minute slot, refreshes a retained force action's timer when a new slot begins, and clips late force commands to the remaining action window. This prevents slow periodic solves and expiring force timers from making batteries switch between self-consumption and force charge partway through the same slot, while price, settings, startup, manual, and safety-triggered changes can still apply immediately.

Update available via HACS
