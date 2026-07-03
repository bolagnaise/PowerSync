<!-- release: v2.12.756 -->

## What's Changed

**Fixed Cheapest EV Smart Schedule windows with timezone-aware forecasts**
PowerSync now converts timezone-aware Amber and Flow Power forecast rows into Home Assistant local time before filtering Smart Schedule hours. This prevents the Cheapest plan from showing no windows when valid overnight forecast slots were supplied in UTC.

**Prevented Hold SoC from replacing the user backup reserve**
Tesla Hold SoC now marks its temporary reserve-floor write as Hold SoC-owned, so ending the hold or returning to Auto no longer leaves the temporary reserve saved as the user's preferred backup reserve.

Update available via HACS
