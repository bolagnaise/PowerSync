<!-- release: v2.12.754 -->

## What's Changed

**Flow Power Happy Hour export planning**
PowerSync now treats a battery export window as profitable when the feed-in tariff is above the stored energy acquisition cost, even if the coincident peak import tariff is higher than the feed-in tariff. This fixes a Flow Power case where today's Happy Hour export window could be dropped shortly before it opened, while the same window tomorrow remained planned because the longer-range import forecast had decayed lower.

Update available via HACS
