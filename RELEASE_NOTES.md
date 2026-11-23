<!-- release: v2.12.1802 -->

## Powerwall protection during scheduled EV charging

PowerSync now keeps an existing Powerwall hold when cloud tariff charging is blocked by the shared EV headroom check. Scheduled charging can also request its battery hold immediately when the optimizer planned to charge the Powerwall but no force charge is active.

## EV charging history

Charging history no longer treats low grid import alone as evidence of solar charging. Total energy still includes intervals where the source is uncertain.

Update through HACS and restart Home Assistant.
