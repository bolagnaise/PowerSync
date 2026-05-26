<!-- release: v2.12.479 -->

## What's Changed

**GloBird ZeroHero optimizer and cost tracking**
PowerSync now has explicit GloBird ZeroHero plan selection for current, legacy, custom, or non-ZeroHero plans. The optimizer models ZeroHero as a capped settlement top-up on top of the normal feed-in tariff, so it no longer treats every 6pm export as equal. Cost tracking now reports the base export value, ZeroHero bonus value, capped kWh used and remaining, no-import credit status, and the daily credit impact.

**Safer Tesla force-mode restore handoff**
Tesla restore-normal handling now avoids an immediate force-mode toggle during tariff sync handoff, clears stale force-toggle retry state after self-consumption restores, and keeps optimizer-owned restores in self-consumption when that is the safe handoff path.

Update available via HACS
