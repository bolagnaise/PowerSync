## What's Changed

**Fix sensors getting stuck on stale local data when the local coordinator dies silently**
The local-prefer sensor override added in 2.12.224 read from the local coordinator's `data` whenever paired and fell back to cloud only when the local value was `None`. If the local coordinator stopped polling for any reason (gateway briefly unreachable, key rejection, unhandled exception in the update loop), its `data` attribute kept the last successful snapshot — and the override happily kept returning that stale value forever, ignoring fresh cloud data on every tick. Mobile app and dashboard tiles would clamp to the moment the local coord died (eg "stuck at 41%"). Now `native_value` requires the local coordinator to have ticked successfully within the last 30 seconds before its data is trusted; otherwise sensors fall through to the cloud coordinator. The local coord polls every 2s so 30s = ~15 missed ticks, well past transient blips.

If you hit the stuck-sensor behaviour: a one-time reload of the PowerSync entry (Settings → Devices → PowerSync → ⋮ → Reload) restores fresh data immediately. After 2.12.227 the freshness guard prevents recurrence.

Update available via HACS
