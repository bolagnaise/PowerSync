<!-- release: v2.12.910 -->

## What's Changed

**Tesla backup-reserve charge kick no longer false-restores**
When Tesla accepts grid charging but repeatedly omits that field from otherwise valid `site_info` readbacks, the 100% backup-reserve charge kick now continues to live charging verification instead of immediately reporting failure and restoring normal operation. Rejected, contradictory, invalid, manual, and incomplete multi-site results remain strictly verified.

**Safer charge-kick command handoff**
Delayed Tesla mode-bounce and verification work now yields safely to newer controls without overwriting them or leaving the Powerwall in the temporary self-consumption mode.

Update available via HACS
