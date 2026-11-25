<!-- release: v2.12.1844 -->

## Powerwall restore no longer fails for tariffs with Sunday-only periods

When a Powerwall restore put your saved tariff back, a time period covering only Sunday, such as weekend off-peak, could be widened to cover the whole week. That period then overlapped your weekday peak times, so Tesla rejected the restore with `Invalid tariff provided` and the forced charge or discharge stayed active. Restores now send the Sunday period unchanged, so it no longer overlaps your other periods.

Update through HACS and restart Home Assistant. If a restore is still pending, press **Restore normal** once after updating.
