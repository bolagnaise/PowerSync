## What's Changed

**Scheduled HACS auto-update for PowerSync**
PowerSync can now install its own HACS updates on a daily schedule, instead of you needing to remember. Enable it from Settings → PowerSync → Configure → "PowerSync auto-update", pick an HH:MM time (defaults to 03:00 local), and once per day at that minute the integration looks for an install-capable HACS update entity that matches PowerSync, calls `update.install`, then restarts Home Assistant after 60 seconds. A new "Auto-Update PowerSync" switch under the controls section also lets you toggle the schedule without opening the options flow, and exposes `scheduled_time`, `last_run`, `last_result` and `last_update_entity` as attributes for visibility.

The scheduler is created on entry setup and torn down on unload, the entity matcher accepts `power_sync`, `powersync`, `power sync` and the legacy `tesla_amber_sync` names so it works whether you upgraded from the old name or installed fresh, and the time field rejects malformed input with a friendly "Enter a valid 24-hour time in HH:MM format" error.

Update available via HACS
