<!-- release: v2.12.1172 -->

## What's Changed

**Cheapest Period no longer starts unplanned solar charging**
Cost Optimized / Cheapest Period now stays within its selected charging
windows instead of switching to Solar Surplus whenever unexpected excess
solar appears. This prevents vehicles ramping to high current outside the
planned cheap period and avoids temporary home-battery contribution.

Planned solar windows continue to work normally, while Solar Preferred and
Solar Only retain opportunistic solar charging. Tesla charging-current
control also continues to respect the selected Home Assistant entity's
limits, including a 1 A positive minimum where supported.

Update available via HACS
