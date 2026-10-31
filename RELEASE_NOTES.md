<!-- release: v2.12.1574 -->

## Tesla Solar Only charging on three phases

Solar Only charging now keeps your configured electrical phases when live charger phase data is missing. Previously, a manual session could fall back to single phase and request too much current on a three-phase charger.

## Manual Tesla Stop

Stop now sends the vehicle's stop request even when its charging sensor still says Stopped or Complete. Previously, that cached status could produce a success message without sending the request. A rejected command reports failure; an accepted request still needs fresh vehicle status or power to confirm charging has stopped.

Update through HACS and restart Home Assistant. Check the Single phase / Three phase setting under Configure > EV load management matches your charging setup; explicit session settings and live charger phase data still take precedence.
