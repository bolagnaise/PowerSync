<!-- release: v2.12.734 -->

## What's Changed

**Tesla free-window force charge no longer blocked by stale price cache**
PowerSync now checks the scheduled action's own tariff slot before applying the Tesla live-solar guard. This fixes a GloBird ZeroHero/free-import edge case where a cached `charge` action could be applied at the start of a `0c` window, but Tesla force charge was still blocked because the cached current-price array had not yet advanced to the free slot.

The live-solar guard still protects Tesla systems during paid import periods, but planned force charge is now allowed during free or negative import windows even when solar is available.

Update available via HACS
