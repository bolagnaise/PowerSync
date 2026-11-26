<!-- release: v2.12.1857 -->

## Powerwall 2 units shown correctly alongside a Powerwall 3

On sites with a Powerwall 3 (with or without expansion packs) plus Powerwall 2 units, the per-pack battery sensors showed only one "Follower Powerwall", often with a near-empty SOC that did not match the real battery, and the other Powerwall 2 was missing. Each Powerwall 2 now gets its own Follower Powerwall entry with its capacity and SOC, and the empty placeholder entry is no longer shown.

## Battery health no longer drops to 0% after an empty Powerwall reply

If the Powerwall briefly returned no battery data, Battery Health could show 0% until the next scan. PowerSync now keeps the previous reading instead. Update through HACS and restart Home Assistant.
