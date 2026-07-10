<!-- release: v2.12.817 -->

## What's Changed

**Backup reserve now follows a single trusted source of truth**
Fixed a family of bugs where PowerSync could pick up a stale Tesla cloud value for your backup reserve and then act on it — the optimizer could plan against an outdated reserve, silently overwrite your saved reserve with a stale one, or restore the wrong value when a Max Backup window ended. Every reserve read is now tagged with where it came from (fresh local reading, fresh cloud, stale cloud, or entity fallback), and PowerSync only saves or adopts values from trusted sources. Max Backup and force windows now restore the reserve you actually set.

**Elevated reserve no longer stranded after EV charging protection or monitoring mode**
When EV charging protection temporarily raised the Powerwall's backup reserve (so the battery doesn't drain into the car), turning the optimizer off — or switching into monitoring mode — could leave the reserve stuck at that elevated level indefinitely. Both paths now put it back: disabling the optimizer restores the previous reserve immediately, and enabling monitoring mode performs a one-time cleanup restore before PowerSync goes hands-off.

**Sigenergy: live backup-reserve changes now survive force cycles**
Changing the backup reserve while PowerSync was running never reached Sigenergy's internal restore target, so the next force-charge/discharge cycle quietly wrote the old reserve back to the inverter. All reserve write paths (mobile app, settings API, optimizer) now keep the restore target in sync, so your new value sticks.

**Mobile app: re-saving unchanged settings no longer blocks the next reload**
Re-saving settings without actually changing anything (backup reserve, AEMO spike settings, tariff provider) could strand an internal flag that then suppressed the next genuine configuration reload. No-op saves are now detected and skipped, so subsequent real changes always apply.

Update available via HACS
