<!-- release: v2.12.1649 -->

## Restore Tesla charging controls through Tessie

PowerSync can again send charging commands through the Tessie integration. This fixes commands being rejected before reaching Tessie, which could cause repeated stop/wake attempts and keep home battery preservation active after EV charging completed.

Update through HACS and restart Home Assistant. Existing recovery holds for uncertain commands remain in place; publication does not confirm recovery on your vehicle.
