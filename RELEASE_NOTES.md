<!-- release: v2.12.1528 -->
## Powerwall charging alerts

Scheduled native charging refusals caused by gateway readings, grid-charging permission or charging/import-limit settings now create a Home Assistant notification and a push to registered PowerSync mobile devices. The message explains why charging did not start.

Repeated retries share one alert. A changed reason updates the Home Assistant message without another push, and charging and discharge alerts clear independently when their checks pass. Routine SOC-limit, EV and ownership deferrals do not create charging alerts.

Update through HACS and restart Home Assistant. Charging and restoration checks remain unchanged.
