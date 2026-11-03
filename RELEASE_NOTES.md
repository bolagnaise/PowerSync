<!-- release: v2.12.1608 -->

## Recover blocked Powerwall settings

EV charging settings and battery-level saves now preserve Powerwall recovery records when they happen at the same time. Previously, an EV planner save could erase the record and leave Powerwall settings blocked with a "recovery owner changed" error.

Supervised recovery also works when an incident is still open but its saved record is missing, instead of repeatedly failing with "Powerwall recovery state changed".

Recovery still requires the current incident IDs and explicit acknowledgment. It preserves the original restore evidence and keeps local settings writes suspended; it does not confirm that the earlier device command finished. Update through HACS and restart Home Assistant, then use the current Repairs notice if settings remain blocked.
