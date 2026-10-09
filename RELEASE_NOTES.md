<!-- release: v2.12.1368 -->

## What's Changed

**Recover GloBird portal sensors after a transient startup failure**

GloBird account, billing, usage, and readiness sensors now keep a provider-only coordinator alive when the portal rejects an uncached first refresh. PowerSync retries with bounded backoff, attaches account and service entities when the portal recovers, and cancels the retry cleanly on unload or reload. Tariff pricing and battery-control paths are unchanged.

Update available via HACS
