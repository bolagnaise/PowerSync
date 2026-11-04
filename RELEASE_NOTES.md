<!-- release: v2.12.1625 -->

## Tesla EV Stop after a session change

Tesla EV Stop now remains available after a charging session changes, expires or reloads when an earlier native current command has an unknown outcome. PowerSync still checks the exact vehicle and provider; it retains the uncertain command record, so Start and current adjustments may remain on hold.

Update through HACS and restart Home Assistant. Leave saved recovery records unchanged.

## Powerwall charge recovery after re-pairing

PowerSync can now finish an expired Native Max charge recovery left behind by an older pairing when the saved site ownership and fresh settings and power readings confirm that charging cleanup has finished. This recovery sends no battery commands; uncertain commands, changed sites or unconfirmed readings keep recovery pending.

Update through HACS and restart Home Assistant. Keep the saved recovery record in place so startup recovery can check it.
