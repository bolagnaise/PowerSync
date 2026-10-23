<!-- release: v2.12.1473 -->
## Powerwall on-grid solar curtailment

A new `power_sync.powerwall_set_on_grid_solar_curtailment` action lets a paired LAN gateway allow or disallow solar reduction while connected to the grid. PowerSync confirms the saved setting through gateway readback, and automations can use the same action.

Use it alongside a zero/limited-export policy. This permission persists until explicitly changed; it does not switch off all PV or directly control a separate AC-coupled inverter. Gateway readback confirms the setting, not a reduction in solar production. There is no Fleet or tariff fallback for this action.
