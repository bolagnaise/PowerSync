<!-- release: v2.12.875 -->

## What's Changed

**Correct Flow Power tariff boundaries during staggered forecast refreshes**
PowerSync now keeps KWatch's available five-minute prices until the first non-overlapping 30-minute forecast interval begins. This prevents a tariff change from temporarily appearing half an hour late in the optimizer chart and schedule when the two KWatch forecast feeds refresh at slightly different times, while preserving the full 30-minute forecast horizon.

Update available via HACS
