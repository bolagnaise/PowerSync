<!-- release: v2.12.1701 -->

## AGL Battery Rewards import prices

Fixed peak import prices dropping to the off-peak rate during the evening export reward when a tariff also contains an all-day off-peak period. The optimizer and price charts now retain the configured peak import price throughout its full window.

Update through HACS and restart Home Assistant. Unchanged generated reward schedules are repaired automatically using your saved import tariff and reward settings. Hand-edited schedules are preserved; review and save those in the tariff editor if their reward periods overlap incorrectly.
