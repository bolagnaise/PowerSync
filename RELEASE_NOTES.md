<!-- release: v2.12.802 -->

## What's Changed

**Sigenergy custom TOU tariff sync**
Manual and scheduled Sigenergy tariff sync now works for custom/static TOU plans even when there is no live Amber, AEMO, Octopus, or Flow Power price coordinator. PowerSync uses the stored TOU schedule to build the 30-minute Sigenergy Cloud tariff upload instead of skipping with a "no price coordinator available" debug line.

Update available via HACS
