<!-- release: v2.12.1065 -->

## What's Changed

**Mobile alerts now flag expired PowerSync cloud authentication**

When a saved PowerSync or Teslemetry cloud token is rejected and Home Assistant
opens its reauthentication repair, PowerSync now also sends one high-priority
push alert to every registered PowerSync mobile app device. The alert directs
the user to Home Assistant Settings > Repairs and explains that automations and
battery control may be unavailable until the connection is restored.

Persisted mobile push registrations are restored before the first provider
refresh, so an already-expired token is visible even immediately after a Home
Assistant restart. PowerSync independently confirms a rejected proxy bearer
before alerting; transient proxy responses and Tesla Fleet token-refresh
failures keep their existing retry behavior and do not generate false expiry
alerts. Push delivery failures never block Home Assistant's repair flow.

Regression coverage verifies startup ordering, the actionable alert text,
single-alert deduplication, confirmed bearer rejection, and transient/Fleet
authentication behavior.

Update available via HACS
