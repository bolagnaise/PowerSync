<!-- release: v2.12.1614 -->

## Restore saved Powerwall tariffs

PowerSync now converts saved Custom TOU time periods into Tesla's current upload format, including flat all-day tariffs. This fixes a payload problem that can leave tariff restoration pending and block Powerwall settings or a change of battery control method.

Update through HACS and restart Home Assistant. Restoration must still succeed and be confirmed before blocked settings can change.
