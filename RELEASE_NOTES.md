<!-- release: v2.12.1853 -->

## Clearer logs for the EV headroom reservation after a Tesla stop

After PowerSync stops a Tesla, it keeps an EV headroom reservation until fresh vehicle and Powerwall readings confirm the car has stopped. Until then, cloud tariff force charging stays refused. The logs didn't show which check was holding the reservation or when it released.

A refused force charge now logs which check refused it, and the release logs `EV headroom reservation released for ...XXXX` (the last four VIN characters). With PowerSync debug logging on, each cycle that keeps the reservation logs the reason, for example how old the vehicle report is. Scheduled Charging now reports its own unconfirmed Stop as `awaiting vehicle confirmation of PowerSync's Stop` instead of calling it an external session. Charging behavior is unchanged. Update through HACS and restart Home Assistant.
