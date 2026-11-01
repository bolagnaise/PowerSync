<!-- release: v2.12.1589 -->

## Tesla EV charging alongside native battery charging

Starting native Powerwall charging now checks fresh vehicle data for old, inactive EV reservations even when the site already supports simultaneous battery and EV charging. This lets stopped vehicles release retained demand so later EV Starts can use available capacity.

Stopped Teslas that report a small standby voltage can also start using their configured charging voltage and phases, provided fresh vehicle data confirms zero current and power. Active EV ownership and site limits remain enforced. Update through HACS and restart Home Assistant.
