## Powerwall settings

Paired Powerwalls can confirm Grid Charging, operation mode and export permission changes locally, including when Tariff control is selected. Unpaired sites continue using Tesla’s cloud API, with cloud fallback when a local change cannot be confirmed.

Force preparation now saves the original settings before changing them, and pending restoration retains its owner through retries and restart. Settings shown as observed come from readback rather than the requested switch position. Tariff uploads and backup reserve changes continue to use Tesla’s cloud API.
