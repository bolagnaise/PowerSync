<!-- release: v2.12.861 -->

## What's Changed

**Prevented force modes from expiring during an active optimizer window**
PowerSync now keeps one authoritative expiry timer when an optimizer window is extended across a tariff or action boundary. This prevents a superseded Tesla force-charge or force-discharge timer from restoring self-consumption early and briefly interrupting the active plan.

**Hardened timer extensions against callback races**
Queued expiry callbacks now re-check the latest shared expiry before restoring normal operation, and refreshed Tesla timers retain the effective post-upload expiry timestamp.

Update available via HACS
