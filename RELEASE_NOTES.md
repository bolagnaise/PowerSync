<!-- release: v2.12.1566 -->

## Keep solar-backed Super Export running

With Allow Grid Charging off, an active priority export window can now continue using measured solar energy when the plan refreshes after charging. Previously, mixing that solar with older stored energy could make export disappear from the refreshed plan.

The allowance remains limited by measured solar energy, household demand, conversion losses, reserves and site limits. Manual overrides, Cost Neutral and EV source planning keep their existing policies. Update through HACS and restart Home Assistant.
