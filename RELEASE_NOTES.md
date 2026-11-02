<!-- release: v2.12.1600 -->

## Powerwall restore recovery

Fixed a remaining case where Powerwall recovery stayed on “Restoring normal operation” after restarting Home Assistant in Monitoring Mode. Saved tariff cleanup can now reach Tesla and finish when its settings are confirmed.

Update through HACS and restart Home Assistant. Control-method changes still wait while restoration is unconfirmed.
