## What's Changed

**SAJ H2: Fix entity discovery — force_charge now actually charges**
The number entities for `passive_bat_charge_power_input` and `passive_bat_discharge_power_input` were silently not being found since v2.12.183. The stanus74 integration constructs unique_ids with the abbreviated key `bat` (e.g. `saj_passive_bat_charge_power_input`), while HA generates entity_ids from the display name using `battery`. A 2.12.183 change swapped the discovery suffix to match the entity_id form instead of the unique_id form, so `_discover_entities()` skipped both number entities entirely. Force charge wrote nothing to the inverter, leaving the battery in self-consumption while PowerSync's mode tracker showed `force_charge`. Fixed: discovery now uses the `passive_bat_*` suffix that matches the actual unique_id.

Update available via HACS
