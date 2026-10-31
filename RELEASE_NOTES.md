<!-- release: v2.12.1573 -->

## Powerwall backup reserve in automations

Automations now wait for the Backup Reserve number's settings service to finish before starting the next action. Previously, a following Grid Charging or Force Charge/Discharge action could overtake the reserve request, leaving it unfinished even when the automation listed reserve first.

Update through HACS and restart Home Assistant. Keep ordinary settings before the force action in your automation. Tesla settings still need verification; this change does not establish that a grid-charging readback failure has cleared or that the battery is charging.
