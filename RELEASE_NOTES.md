<!-- release: v2.12.1505 -->

## What's Changed

**Powerwall recovery after a rejected tariff upload**

A tariff upload explicitly rejected with HTTP 429 could leave Max charging, export, or restore waiting indefinitely for a command Tesla had refused. PowerSync now distinguishes that single-request rejection, respects the provider's cooldown, and allows restoration to retry while retaining the checks for requests with unknown outcomes.

Existing recovery records from earlier versions remain protected until their outcome is verified; updating does not automatically clear an uncertain restore. Update through HACS and restart Home Assistant.

Update available via HACS
