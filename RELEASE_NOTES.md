## What's Changed

**Mobile app timestamps now show the right moment for Container / Supervised installs**
Companion to v2.12.253's TOU fix: the integration was emitting timestamps like `data_updated_at`, `synced_at`, `scanned_at`, `registered_at`, charging-session `start_time`/`end_time`, and the tariff `last_sync` string as naive ISO strings. The mobile app feeds these into JavaScript's `new Date()`, which interprets a TZ-less ISO string as the phone's local time. On HA Container / Supervised installs (where the container clock is UTC), that meant a phone in Adelaide saw "9 hours ago" the moment after a sync, charging sessions appeared 9.5 hours displaced on the History screen, and "synced X minutes ago" indicators were always wrong. HAOS users were unaffected. All emission sites now produce timezone-aware ISO strings (`2026-04-30T22:57:34+09:30`) that JavaScript parses unambiguously.

**Charging session timestamps backwards-compatible with stored history**
Sessions persisted before this upgrade have naive ISO strings. The session loader now treats any naive timestamp it reads back as HA-local time, so old history keeps rendering correctly alongside new sessions.

**Tariff `last_sync` display string for Tesla/foxess/sungrow paths**
The dashboard's "synced X" indicator pulled from `last_sync`, which was being formatted from the container clock for Tesla custom tariffs and the foxess/sungrow tariff bridges. Those paths now format from HA's configured timezone.

Update available via HACS
