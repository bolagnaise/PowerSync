<!-- release: v2.12.1349 -->

## What's Changed

**Explicit Tesla BLE Boost commands are no longer dropped by automatic wake backoff**

Immediate Boost and other explicit user starts now carry a transient user-command marker through the initial Tesla BLE wake, charge-limit, and phase-safe current writes. The requested vehicle can bypass a failed-bridge backoff armed by an automatic loop, while periodic updates and other vehicles retain their normal fail-closed backoff and ownership, freshness, and phase-budget safeguards.

**EV stop notifications now reflect the physical stop result**

Dynamic-session cleanup no longer sends a stopped push notification before attempting the charger stop. The notification is delayed until the stop path returns success; failed or unconfirmed stops keep the controller cleanup and release behavior but emit an accurate diagnostic instead of claiming that charging stopped.

Update available via HACS
