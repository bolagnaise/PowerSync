<!-- release: v2.12.1507 -->

### Powerwall recovery after a blocked tariff upload

Max charging or export could stay stuck in recovery when the final safety check stopped a tariff request before it was sent to Tesla. PowerSync now records that the request was not sent and completes the usual restoration checks, including when the request was cancelled while that check was running. Requests with an unknown outcome remain protected.

Update through HACS and restart Home Assistant. This prevents new cases; it does not automatically clear an older saved recovery record. If an earlier request is already stuck, retain its recovery file and contact support with logs covering the original request.
