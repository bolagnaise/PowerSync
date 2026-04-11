## What's Changed

**Fix: Solcast forecast no longer resets to a partial-day number after a mid-day restart**
When a user restarted Home Assistant partway through the day, the Solcast coordinator lost its in-memory full-day forecast cache and the next fetch fell into a fallback path that cached `today_remaining` (just the rest-of-day forecast) as if it were the full-day total. The "today's forecast" sensor would then show a suspiciously low value like `2.3 kWh` until the next UTC day rollover — which looked indistinguishable from rate limiting and caused confusion about whether Solcast was broken.

The coordinator now persists the in-memory full-day cache (`_daily_forecast_date`, `_daily_forecast_kwh`, `_daily_forecast_peak_kw`) to the same forecast store that already handles rate-limit counters, and restores it on first update after startup. If the restored cache is from today's date, the fetch logic skips the "new day" fallback entirely and keeps reporting the full-day total. A warning is also logged if the coordinator ever does have to cache a suspiciously small `today_remaining` value as the full-day forecast after 10:00 local time, so this class of bug is visible in logs instead of just showing up as a wrong sensor reading.

Thanks to the user who shared the diagnostic log that surfaced this — the `Solcast: New day, cached forecast for 2026-04-11: 2.3kWh` line was the giveaway.

Update available via HACS
