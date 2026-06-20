<!-- release: v2.12.680 -->

## What's Changed

**Separate grid export limit for Smart Optimization**
PowerSync now has a dedicated Maximum grid export setting for Smart Optimization. The battery/inverter discharge limit remains the physical capability used to cover house load, while the new grid export cap limits only planned export and exact target-export commands. This fixes systems where a DNSP/site export limit, such as 5.5 kW, should not prevent a larger inverter from covering home load and exporting at the same time.

**Clear zero-export and API semantics**
The optimization settings API now accepts `max_grid_export_w`: omit it to leave the existing setting unchanged, send `null` or blank to clear the explicit PowerSync cap and fall back to legacy detection, send `0` for a real zero-export site, or send a positive watt value for a configured site export cap. The Home Assistant config/options flows expose the same setting as Maximum grid export.

**Spread export respects site caps without shrinking inverter capacity**
Spread Export Across Window now flattens export using the effective grid export cap instead of the physical discharge limit. Target-export battery systems receive capped export targets, while coarse-control systems keep using physical discharge commands where the runtime cannot safely enforce an exact export wattage.

**Dual Sungrow grid export support**
Dual Sungrow systems can now force grid export by driving both inverters at their physical discharge split while applying the export limit on the primary grid-facing inverter. Restore and failure paths unwind discharge limits and the primary export limit so temporary export control does not leave the system in a partial state.

**Sungrow history relink tools**
Sungrow users migrating from mkaiser sensors can now preview and apply supported daily-energy entity relinks through new PowerSync services and the `/api/power_sync/history_relink` endpoint. The relink preserves recorder history by moving entity IDs in the entity registry and records applied mappings so later canonical entity migration skips those relinked sensors.

Update available via HACS
