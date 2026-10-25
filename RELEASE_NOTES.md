<!-- release: v2.12.1503 -->
### Powerwall Tariff control without a local connection

Selecting Tariff now uses Tesla cloud control for Max and optimiser charging/export, even when a paired Powerwall cannot be reached over the LAN. Previously, a local connection error could prevent the cloud tariff path from starting.

Update through HACS and restart Home Assistant. Numeric power targets still require Local V1R; cloud confirmation and unfinished-control recovery checks still apply. This fix does not repair a local network connection or confirm physical charging.
