<!-- release: v2.12.1141 -->

## What's Changed

**A blank Maximum grid export no longer silently disables Profit Max solar export**
Profit Max can hold battery charging so high-priced solar exports instead, but only once it knows your site export cap. The **Maximum grid export** field said "leave blank to auto-detect where available", and in practice only Sigenergy and AlphaESS ever reported one. On every other control path a blank field switched the feature off permanently — every candidate slot fell back to self-consumption, with no log line, no repair, and nothing to see but a nested field on `sensor.power_sync_optimization_status`.

**Fronius GEN24 export limits are now auto-detected**
PowerSync reads the `fronius_modbus` **Export soft limit** sensor and uses it as the site export cap. That sensor only exists when the Fronius Web API is configured in the companion integration, and only reports a value while Export Limit Control's soft limit is enabled on the inverter — so a reading is an authoritative cap, not a guess. If you do not have it, set Maximum grid export yourself.

**The refusal now tells you what to do about it**
When nothing upstream reports a cap and the field is blank, PowerSync logs a warning naming the exact setting and raises a Home Assistant repair. The status reason is also split: `export_limit_not_configured` for a blank field, `zero_export_site` for a deliberate 0. Both previously reported `finite_export_limit_required`, which told zero-export sites to set a limit they had already set.

The setting's help text and the Smart Optimization and Fronius wiki pages have been corrected to match. No control behaviour changed — the hold stays fail-closed until a finite export cap is known.

Update available via HACS
