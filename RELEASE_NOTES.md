<!-- release: v2.12.1504 -->

## What's Changed

**Powerwall Automatic export after a slow gateway response**

Automatic now obtains fresh gateway readings when confirming an export limit took too long, instead of abandoning a correctly confirmed limit because the earlier readings had expired. Changed limits, unavailable readings, and other safety checks still block export; this fixes one handoff failure and does not establish the cause of every stop/start report.

Update through HACS and restart Home Assistant.

Update available via HACS
