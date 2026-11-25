<!-- release: v2.12.1838 -->

## No more export-price warning for solar EV charging at night

When Solar Surplus EV charging was paused because the export price was above your limit, the car's status showed that reason even at night, for example "Export value 8.0c/kWh exceeds Solar Surplus limit 0.0c/kWh". In the app it looked like a problem. When there is no solar generation, the status now says "No solar generation right now". Charging works the same as before; only the message has changed. During the day, a high export price is still reported as before.

Update through HACS and restart Home Assistant.
