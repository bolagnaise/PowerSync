## What's Changed

**Powerwall commands go local for paired sites**
Backup reserve, operation mode, and grid export rule now write directly to the gateway over the LAN via signed V1R `write_config` calls, skipping the Tesla cloud round-trip entirely. Toggling these in the dashboard goes from 1-10 second latency (Fleet API + Tesla queue + retry on 429s) to sub-100ms, and keeps working through Tesla cloud outages and rate-limit windows. Cloud Fleet API stays in place as automatic fallback if the gateway is unreachable or the RSA key gets rejected, and unchanged for unpaired sites.

**Local telemetry poll dropped to 2 seconds**
Battery power, grid power, SOC, and grid status now refresh every 2s for paired sites, down from the previous 10s. The old cadence was sized around Fleet API rate limits; once the data path is local those limits don't apply. The gateway samples at ~1Hz natively so 2s is the floor that gives near-real-time updates without re-asking for the same value.

**Storm watch, grid charging, and TOU tariff stay cloud-only**
Empirically confirmed against PW3 firmware 26.2.1: `storm_mode_enabled` and `disallow_charge_from_grid_with_solar_installed` aren't in the gateway's local `config.json` (Tesla holds them server-side), and the V1R protobuf has no `setTariff` message or tariff FileStore domain. Tariff sync, storm watch toggles, and grid charging toggles continue to use the Fleet API path unchanged.

Update available via HACS
