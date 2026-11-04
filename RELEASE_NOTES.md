<!-- release: v2.12.1624 -->

## Powerwall charge recovery after re-pairing

PowerSync can now finish an expired Native Max charge recovery left behind by an older pairing when the saved site ownership and fresh settings and power readings confirm that charging cleanup has finished. This recovery sends no battery commands; uncertain commands, changed sites or unconfirmed readings keep recovery pending.

Update through HACS and restart Home Assistant. Keep the saved recovery record in place so startup recovery can check it.
