<!-- release: v2.12.1486 -->
## Powerwall Max discharge

Fixed Max discharge on paired Powerwall systems above 10 kW being blocked by a slightly higher gateway export limit or silently falling back to a 10 kW local command. PowerSync now temporarily tightens a looser gateway export limit to your configured export limit, confirms it before uploading the forcing tariff, and restores the original limit after discharge settles. External limit changes are preserved. If takeover cannot be confirmed, Max reports an error.

## Powerwall optimiser tariff control

Fixed native tariff actions being rejected when a rounded inverter rating or charge/discharge planning rate was below the reported site rating. Reserve and charge-ceiling checks use the higher reported/configured rating, and shortened action deadlines are saved without another startup command or extending the action.

Software SOC reserves still apply: an export window may be shortened to protect the reserve. Update through HACS and restart Home Assistant.
