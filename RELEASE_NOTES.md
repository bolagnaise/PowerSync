<!-- release: v2.12.923 -->

## What's Changed

**Tesla manual Force Charge compatibility**
Manual Force Charge now continues when Tesla accepts the grid-charging command but current Powerwall firmware consistently omits that setting from otherwise valid direct `site_info` readbacks. Rejected writes, invalid or unconfirmed responses, incomplete multi-site coverage, and Force Discharge remain fail-closed.

Update available via HACS
