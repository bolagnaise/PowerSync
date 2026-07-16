<!-- release: v2.12.859 -->

## What's Changed

**Battery Health shows the physical pack count when Tesla relay detail is partial**
The Battery Health card now uses the validated aggregate `battery_count` for its pack total instead of shrinking to the number of per-pack rows returned by that relay scan. When Tesla omits individual detail for one or more packs, the card keeps the verified rows, clearly states how many packs were reported individually, and does not invent a pack role or capacity.

Update available via HACS
