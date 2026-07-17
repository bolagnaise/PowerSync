<!-- release: v2.12.879 -->

## What's Changed

**Keep capped tariff export accounting current during long optimiser runs**

PowerSync now advances live cost and quota settlement on every five-minute coordinator refresh, even when a previous optimiser solve or force-export window is still active. This prevents GloBird ZeroHero's 15 kWh Super Export allowance from being under-counted while the battery is exporting, so the next plan receives the correct remaining allowance instead of continuing from a stale value.

The same refresh keeps daily import/export and cost tracking current without double-counting when a normal optimiser solve completes.

Update available via HACS
