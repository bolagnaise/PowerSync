## What's Changed

**SAJ H2: Force Charge/Discharge Now Actually Works**
The previous implementation tried to control passive mode by writing `passive_charge_enable` and `app_mode` as number entities. Two bugs combined to make this fail silently: (1) stanus74's number entity handler for `passive_charge_enable` writes the register directly without touching AppMode, so the inverter entered an inconsistent state (passive_enable set but AppMode still 0). (2) The `"app_mode"` key was already claimed by the sensor entity discovered first — the subsequent write to the number entity was resolved to the sensor's entity_id, which HA's `number.set_value` service silently ignored. AppMode never changed to 3, so the inverter treated all passive mode commands as if they weren't there.

The fix is to use the passive switch entities (`passive_charge_control`, `passive_discharge_control`) that stanus74 provides specifically for this purpose. Turning on these switches triggers stanus74's internal `_activate_passive_mode()`, which captures the current AppMode and sets AppMode=3 atomically before writing `passive_charge_enable`. Turning them off triggers `_deactivate_passive_mode()`, which restores AppMode automatically. Power targets are still set via the number entities before activating the switch.

Update available via HACS
