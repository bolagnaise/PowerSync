<!-- release: v2.12.927 -->

## What's Changed

**AGL Battery Rewards supplier support**
Added AGL Battery Rewards as a first-class Australian electricity provider. PowerSync now models the plan's daily 17:00-21:00 premium feed-in window and its off-peak feed-in rate across configuration, tariff sensors, optimiser pricing, EV planning, and the mobile provider settings API.

**Address-specific rates remain editable**
The AGL export rates default to 28 c/kWh during the reward window and 3 c/kWh outside it, but can be changed as offers evolve. Users enter the import rates from their own AGL Energy Price Fact Sheet; PowerSync preserves those flat or time-of-use import periods while automatically applying the Battery Rewards export schedule.

Update available via HACS
