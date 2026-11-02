<!-- release: v2.12.1596 -->

## What's Changed

**Powerwall charging after disabling Tesla schedule rate control**
Turning off rate control for the last vehicle now clears an outdated charging block, even when no vehicle is connected. Real or unconfirmed EV reservations still wait for confirmed cleanup.

Update through HACS and restart Home Assistant.

Update available via HACS
