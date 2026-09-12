<!-- release: v2.12.1280 -->

## What's Changed

**Prevent stale dynamic-price solar export at a tariff boundary**
PowerSync now waits for the settled current feed-in interval before applying a cached Solar Export hold on dynamic tariffs. This prevents an earlier forecast from temporarily blocking battery charging when the live export price has changed at the boundary.

Update available via HACS
