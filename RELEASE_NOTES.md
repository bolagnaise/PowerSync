<!-- release: v2.12.1630 -->

## Reliable Octopus price updates

BottlecapDave Octopus rate, account-event and entity updates now refresh PowerSync without Home Assistant thread-safety errors. Startup and scheduled price-boundary refreshes use the same corrected handling.

Update through HACS and restart Home Assistant. This fixes the reproduced refresh failure; it does not establish the cause of reported Powerwall charging cycles. Charging still depends on your selected control method, battery limits and permissions.
