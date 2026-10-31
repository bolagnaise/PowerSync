<!-- release: v2.12.1577 -->

## Battery planning when a Tesla vehicle goes offline

A Tesla vehicle becoming unavailable during a charging-schedule read could stop regular battery replanning, leaving PowerSync following an older export or charging plan. PowerSync now keeps replanning and checks other vehicles while retaining the last confirmed schedule within its freshness limits.

Update through HACS and restart Home Assistant. This fixes the planning interruption; it does not clear an unresolved Powerwall local-settings recovery warning or guarantee a particular export total.
