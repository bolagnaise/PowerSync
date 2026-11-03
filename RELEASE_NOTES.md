<!-- release: v2.12.1609 -->

## Correct AGL/Ausgrid seasonal import times

The automatic AGL/Ausgrid preset now uses peak import from 3–9 pm every day in November–March and June–August. April–May and September–October are all off-peak. The previous preset used different hours, weekdays and months, which could look like a daylight-saving error when peak pricing resumed on 1 November.

Unchanged automatic presets are repaired on startup while keeping entered prices. Manual or edited tariffs keep their saved definitions; compare those with your account's offer. The separately configured evening export reward remains 5–9 pm.

## Keep seasonal tariff charts current

The TOU chart now switches its displayed hours and rates when the tariff season changes, without waiting for a tariff sync or reload. Its cache also refreshes when the local date or daylight-saving offset changes.

Update through HACS and restart Home Assistant to apply the preset repair.
