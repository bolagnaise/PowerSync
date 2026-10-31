<!-- release: v2.12.1577 -->

## Battery planning when a Tesla vehicle goes offline

A Tesla vehicle becoming unavailable during a charging-schedule read could stop regular battery replanning, leaving PowerSync following an older export or charging plan. PowerSync now keeps replanning and checks other vehicles while retaining the last confirmed schedule within its freshness limits.

Update through HACS and restart Home Assistant. This fixes the planning interruption; it does not clear an unresolved Powerwall local-settings recovery warning or guarantee a particular export total.

## Coordinated Tesla battery and vehicle controls

Powerwall controls and EV charging now coordinate changes to the same energy site, reducing conflicts between tariff changes, temporary controls and recovery. Vehicle Boost timers retain each car's original deadline across reloads, and pending cleanup remains until matching vehicle readings confirm completion.

## Two vehicles on the energy flow picture

The generated energy flow picture can now show two plugged-in vehicles at home, with separate names, charge levels and power readings. Selecting one vehicle keeps the other visible; existing standalone cards retain their manual entity settings.
