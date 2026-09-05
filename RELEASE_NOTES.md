<!-- release: v2.12.1236 -->

## What's Changed

**Bind the grid role to the real grid meter on Home Assistant monitoring profiles**

Auto-discovery for monitoring-only Home Assistant battery profiles could bind the
`grid_power` role to a plant or inverter AC-output entity instead of the grid
meter. Sites then saw a large positive (import) grid value during an unbroken
export, and a Home Load inflated by roughly the whole plant output, because Home
Load is reconstructed as solar + grid + battery.

The cause was a brand-agnostic `active_power` alias added in v2.12.1225 for the
GoodWe entity bridge. It matched any entity whose name ended in `active_power`,
so a Sigenergy `plant`/`inverter` AC-power entity outranked the grid meter and
won an alphabetical tiebreak. Sites on v2.12.1225 through v2.12.1235 with such an
entity are affected.

This release:

- restricts the generic `active_power` alias so it cannot capture entities
  qualified as plant, inverter, ESS, battery, PV/solar, load or backup, while
  still matching the GoodWe `sensor.goodwe_active_power` bridge it was added for;
- matches aliases on whole-word boundaries, so a `reactive_power` entity is no
  longer treated as an `active_power` grid candidate;
- breaks ties in favour of grid-specific entities (grid, meter, import, export)
  rather than alphabetically;
- applies the export-entity sign correction on every brand, not just Sungrow, so
  a grid role resolved to an export sensor reads negative while exporting. This
  also restores the Sungrow correction, which the same generic alias could
  silently disable;
- stops a connection profile's own declared `grid_power` multiplier from being
  overwritten when discovery did not compute one.

Monitoring-only profiles remain monitoring-only: nothing here adds hardware
control or changes inverter operating modes.

Update available via HACS
