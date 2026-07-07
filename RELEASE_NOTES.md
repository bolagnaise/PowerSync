<!-- release: v2.12.785 -->

## What's Changed

**Add weekend-only custom TOU periods**
Custom TOU setup now includes a Weekends only (Sat-Sun) option when adding tariff periods. This supports retailers such as Red Energy where weekday peak/shoulder/off-peak rules differ from Saturday and Sunday rates.

**Fill off-peak gaps per day**
Custom TOU schedules now build remaining off-peak windows independently for weekdays, Saturdays, and Sundays. Mixed weekday/weekend tariffs no longer have to fake weekend rates with all-day periods or separate single-day entries.

Update available via HACS
