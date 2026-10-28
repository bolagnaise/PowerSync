<!-- release: v2.12.1527 -->
## Powerwall discharge alerts

PowerSync now shows a Home Assistant notification when scheduled native discharge cannot start because the gateway's Grid Export setting cannot be confirmed. Registered PowerSync mobile devices also receive a push notification.

Repeated retries share one alert, which clears when a later native discharge passes the check. The existing discharge checks remain in place.

Update through HACS and restart Home Assistant. This adds failure notifications; it does not resolve an unavailable gateway setting.
