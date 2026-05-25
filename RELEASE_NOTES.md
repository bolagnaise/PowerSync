<!-- release: v2.12.471 -->

## What's Changed

**Sigenergy tariff sync station IDs**
Sigenergy tariff uploads now preserve alphanumeric station IDs instead of converting them to numbers. This fixes tariff sync failures for systems whose station ID contains letters, including the Home Assistant error `invalid literal for int() with base 10`.

**Calendar history support logging**
PowerSync now logs summarized kWh totals for energy-summary calendar-history responses, including the source system, period, row count, and key energy totals. This gives support enough detail to diagnose mobile calendar-history discrepancies without exposing every recorder row.

Update available via HACS
