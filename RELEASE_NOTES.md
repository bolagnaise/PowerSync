<!-- release: v2.12.1608 -->

## Recover blocked Powerwall settings

Supervised Powerwall settings recovery now works when an incident is still open but its saved recovery record is missing. Previously, **Resume Powerwall settings through cloud** could keep failing with "Powerwall recovery state changed" in this situation.

Recovery still requires the current incident IDs and explicit acknowledgment. It preserves the original restore evidence and keeps local settings writes suspended; it does not confirm that the earlier device command finished. Update through HACS and restart Home Assistant, then use the current Repairs notice if settings remain blocked.
