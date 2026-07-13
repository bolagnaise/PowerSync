<!-- release: v2.12.839 -->

## What's Changed

**Per-vehicle EV battery capacity planning**

Smart Schedule now resolves usable battery capacity independently for every identified EV, using a per-vehicle override, explicit provider data, an exact model and trim estimate, or the existing 60 kWh compatibility estimate. Required charging energy, selected windows, ETA, serialized plans, and the EV demand seen by Smart Optimization now use the same resolved value. Multi-EV households can therefore plan vehicles with different battery sizes without one vehicle's capacity affecting another.

**Generic and OCPP charger fallback capacity**

Anonymous Generic and OCPP loadpoints can use one shared usable-capacity fallback configured in PowerSync. The Home Assistant options flow, EV APIs, Smart Schedule status, normalized loadpoint responses, and dashboard now expose the effective capacity and whether it is configured, provider-reported, model-estimated, or using the default estimate.

**Inline Home Assistant dashboard editing**

Each Smart Schedule vehicle row now includes a compact decimal capacity editor. Saving a capacity regenerates only the matching vehicle plan and schedules a coalesced optimizer refresh without issuing a charger start, stop, or current command. Clearing the value restores automatic resolution.

Update available via HACS
